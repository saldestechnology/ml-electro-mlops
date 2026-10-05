"""Prefect flows run on the VPS (M6). `pricefc serve` registers them with their schedules.

Each flow is a thin wrapper around the same functions the CLI uses, so a flow run and a manual
command produce identical snapshots and MLflow runs.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from prefect import flow, task

CONFIG = Path("configs/base.yaml")
INGEST = Path("configs/ingest.yaml")
FEATURES = Path("configs/features.yaml")


def _base() -> Any:
    from pricefc.config import load_config

    return load_config(CONFIG)


@task(retries=3, retry_delay_seconds=300)
def ingest_prices_task(lookback_days: int = 3) -> dict[str, int]:
    """Pull the current (and, early in a month, the previous) month of prices. Resumable."""
    from pricefc.config import load_ingest_config
    from pricefc.ingest.prices import ElprisSource
    from pricefc.ingest.run import ingest_prices, log_ingest_run

    base = _base()
    ing = load_ingest_config(INGEST)
    now = datetime.now(ZoneInfo(base.timezone))
    today = now.date()
    # Tomorrow's prices are published around 13:00 local; ask for them only after that.
    published = (now.hour, now.minute) >= (13, 15)
    results = ingest_prices(
        base,
        ElprisSource(ing.elprisetjustnu),
        list(base.zones),
        today - timedelta(days=lookback_days),
        today + timedelta(days=1) if published else today,
        force=True,
    )
    if results:
        log_ingest_run(
            base, results, zones=list(base.zones), label="prices-daily", weather_source="none"
        )
    return _summary(results)


@task(retries=3, retry_delay_seconds=300)
def ingest_weather_task(endpoint: str, lookback_days: int = 7) -> dict[str, int]:
    from pricefc.config import load_features_config, load_ingest_config
    from pricefc.ingest.openmeteo import weather_source_tag
    from pricefc.ingest.run import ingest_weather, locations_for, log_ingest_run

    base = _base()
    ing = load_ingest_config(INGEST)
    locs = locations_for(load_features_config(FEATURES), list(base.zones))
    start = None if endpoint == "forecast" else date.today() - timedelta(days=lookback_days)
    results = ingest_weather(base, ing, locs, endpoint, start)  # type: ignore[arg-type]
    log_ingest_run(
        base,
        results,
        zones=list(base.zones),
        label=f"weather-{endpoint}-daily",
        weather_source=weather_source_tag(endpoint, ing.open_meteo.model),
    )
    return _summary(results)


def _summary(results: list[Any]) -> dict[str, int]:
    failed = sum(not r.report.passed for r in results)
    return {"snapshots": len(results), "failed": failed}


@flow(name="ingest-daily", log_prints=True)
def ingest_daily() -> dict[str, dict[str, int]]:
    """Refresh prices and weather (archives, previous runs, live forecast as issued)."""
    out = {
        "prices": ingest_prices_task(),
        "previous_runs": ingest_weather_task("previous_runs"),
        "historical_forecast": ingest_weather_task("historical_forecast"),
        "forecast": ingest_weather_task("forecast"),
    }
    failed = {k: v["failed"] for k, v in out.items() if v["failed"]}
    if failed:
        # Snapshots are kept and marked invalid; fail the run so it is visible and alerts.
        raise RuntimeError(f"validation failed: {failed}")
    print(f"ingest ok: {out}")  # captured by Prefect (log_prints)
    return out


def deployments(env: str) -> list[Any]:
    """Scheduled deployments for one environment (cron in Europe/Stockholm)."""
    from prefect.schedules import Cron

    tz = "Europe/Stockholm"
    # Staging runs ahead of production so the two never compete for memory.
    morning = os.environ.get("PRICEFC_CRON_MORNING", "15 8 * * *")
    afternoon = os.environ.get("PRICEFC_CRON_AFTERNOON", "30 13 * * *")
    return [
        ingest_daily.to_deployment(
            name=f"{env}-{slot}", schedule=Cron(cron, timezone=tz), tags=[env]
        )
        for slot, cron in (("morning", morning), ("afternoon", afternoon))
    ]


def serve() -> None:
    """Register the deployments and run them (blocking)."""
    from prefect import serve as prefect_serve

    prefect_serve(*deployments(os.environ.get("PRICEFC_ENV", "dev")))
