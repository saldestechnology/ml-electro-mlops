"""Prefect flows run on the VPS (M6). `pricefc serve` registers them with their schedules.

Each flow is a thin wrapper around the same functions the CLI uses, so a flow run and a manual
command produce identical snapshots and MLflow runs. A failed or crashed flow run sends a
Telegram alert (best effort, see `pricefc.alerts`).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from prefect import flow, task

CONFIG = Path("configs/base.yaml")
INGEST = Path("configs/ingest.yaml")
FEATURES = Path("configs/features.yaml")
log = structlog.get_logger(__name__)


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
    # forecast and single_runs default to the hours a forecast made today needs.
    live = endpoint in ("forecast", "single_runs")
    start = None if live else date.today() - timedelta(days=lookback_days)
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


def _alert(flow: Any, flow_run: Any, state: Any) -> None:
    """on_failure / on_crashed hook: flow, run and a short exception summary, no traceback."""
    try:
        from pricefc.alerts import send_telegram

        summary = (getattr(state, "message", None) or str(getattr(state, "name", "?"))).strip()
        summary = summary.splitlines()[-1] if summary else "?"
        send_telegram(
            f"{flow.name} {getattr(state, 'name', 'failed')}: run {flow_run.name} "
            f"({flow_run.id})\n{summary[:500]}"
        )
    except Exception:  # an alert must never fail the hook chain
        pass


@flow(name="ingest-daily", log_prints=True, on_failure=[_alert], on_crashed=[_alert])
def ingest_daily() -> dict[str, Any]:
    """Refresh prices and weather (archives, previous runs, live forecast as issued)."""
    out = {
        "prices": ingest_prices_task(),
        "previous_runs": ingest_weather_task("previous_runs"),
        "historical_forecast": ingest_weather_task("historical_forecast"),
        "forecast": ingest_weather_task("forecast"),
    }
    out["silver"] = _build_silver_best_effort()
    failed = {k: v["failed"] for k, v in out.items() if v.get("failed", 0)}
    if failed:
        # Snapshots are kept and marked invalid; fail the run so it is visible and alerts.
        raise RuntimeError(f"validation failed: {failed}")
    print(f"ingest ok: {out}")  # captured by Prefect (log_prints)
    return out


@task(retries=1, retry_delay_seconds=300)
def score_forecasts_task() -> list[dict[str, Any]]:
    """Score complete published forecasts and return a compact flow result."""
    from pricefc.serving.score import score_forecasts

    scores = score_forecasts(_base())
    return [
        {
            "zone": item.zone,
            "role": item.role,
            "origin_date": item.origin_date.isoformat(),
            "pinball": item.pinball,
            "coverage_90": item.coverage_90,
            "mae_median": item.mae_median,
        }
        for item in scores
    ]


@flow(name="score-daily", log_prints=True, on_failure=[_alert], on_crashed=[_alert])
def score_daily() -> dict[str, Any]:
    """Refresh prices best effort, then score any fully published recent forecasts."""
    try:
        ingest: dict[str, Any] = ingest_prices_task()
    except Exception as exc:
        ingest = {"error": _short(exc)}
    if ingest.get("failed", 0):
        ingest["warning"] = f"{ingest['failed']} invalid price snapshot(s)"

    scores = score_forecasts_task()
    out = {"ingest": ingest, "scored": len(scores), "scores": scores}
    print(
        f"score-daily: {len(scores)} new scores; "
        f"price ingest {ingest.get('snapshots', 'failed')}, "
        f"invalid {ingest.get('failed', 'n/a')}"
    )
    return out


@task(retries=1, retry_delay_seconds=300)
def build_datasets_task(zone: str) -> dict[str, str]:
    """Rebuild the training (stitched) and evaluation (true_lead) datasets from the latest
    snapshots; identical content on the same day reuses the stored version."""
    from pricefc.config import load_features_config, load_ingest_config
    from pricefc.datasets.build import build_dataset

    base = _base()
    feats, ing = load_features_config(FEATURES), load_ingest_config(INGEST)
    out = {}
    for kind in ("stitched", "true_lead"):
        stored, _ = build_dataset(base, feats, ing, zone, kind)
        out[kind] = stored.version
    return out


@task(retries=1, retry_delay_seconds=300)
def forecast_zone_task(
    zone: str,
    day: date,
    model: str = "ensemble_hourly_exp",
    alias: str = "champion",
    role: str = "champion",
) -> dict[str, Any]:
    """Live rows for the origin, then one served role's forecast (idempotent per day)."""
    from pricefc.config import load_features_config, load_ingest_config
    from pricefc.datasets.live import build_live_rows
    from pricefc.serving.live import forecast_origin

    base = _base()
    feats, ing = load_features_config(FEATURES), load_ingest_config(INGEST)
    rows = build_live_rows(base, feats, ing, zone, day)
    res = forecast_origin(
        base, zone, day, model=model, alias=alias, role=role, live=rows, features=feats, ingest=ing
    )
    return {
        "version": res.model_version,
        "role": role,
        "rows": len(res.forecasts),
        "caught_up": [d.isoformat() for d in res.caught_up],
        "path": str(res.path),
    }


WEATHER_FOR_FORECAST = ("previous_runs", "historical_forecast", "single_runs")


def _short(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {text[0][:300] if text else ''}"


@task(retries=1, retry_delay_seconds=300)
def build_silver_task() -> dict[str, Any]:
    """Refresh the cleaned lake from all valid raw snapshots."""
    from pricefc.lake import build_silver

    return build_silver(_base())


def _build_silver_best_effort() -> dict[str, Any]:
    """Run the silver task and report failures without stopping ingestion or forecasts."""
    try:
        return build_silver_task()
    except Exception as exc:
        summary = _short(exc)
        log.error("silver_build_failed", error=summary)
        try:
            from pricefc.alerts import send_telegram

            send_telegram(f"silver build failed: {summary[:500]}")
        except Exception:
            pass
        return {"error": summary}


def _alert_challenger_failure(zone: str, day: date, alias: str, exc: BaseException) -> None:
    """Log and alert a shadow failure without changing the champion flow result."""
    summary = _short(exc)
    log.error(
        "challenger_forecast_failed", zone=zone, day=day.isoformat(), alias=alias, error=summary
    )
    try:
        from pricefc.alerts import send_telegram

        send_telegram(
            f"forecast-daily challenger failed: {zone} {day.isoformat()} alias={alias}: {summary}"
        )
    except Exception:
        pass


@flow(name="forecast-daily", log_prints=True, on_failure=[_alert], on_crashed=[_alert])
def forecast_daily(day: date | None = None) -> dict[str, Any]:
    """Refresh inputs, serve every champion first, then configured shadow challengers.

    Challenger failures are reported and alerted but do not fail or replace champion results.
    One champion zone failing does not stop the others; ingest or champion errors fail at end.
    """
    base = _base()
    day = day or datetime.now(ZoneInfo(base.timezone)).date()
    errors: dict[str, str] = {}
    ingest: dict[str, Any] = {}
    steps: list[tuple[str, Any, tuple[Any, ...]]] = [("prices", ingest_prices_task, ())]
    steps += [(ep, ingest_weather_task, (ep,)) for ep in WEATHER_FOR_FORECAST]
    for name, fn, args in steps:
        # Stale inputs show up in the live-row checks; a failed pull alone is not fatal here.
        try:
            ingest[name] = fn(*args)
            if ingest[name]["failed"]:
                errors[f"ingest:{name}"] = f"{ingest[name]['failed']} invalid snapshot(s)"
        except Exception as exc:
            errors[f"ingest:{name}"] = _short(exc)
    silver = _build_silver_best_effort()
    zones: dict[str, Any] = {}
    built: dict[str, dict[str, str]] = {}
    for zone in base.zones:
        try:
            datasets = build_datasets_task(zone)
            built[zone] = datasets
            zones[zone] = {"datasets": datasets}
        except Exception as exc:
            errors[zone] = _short(exc)

    served = {item.role: item for item in base.serving.models}
    champion = served["champion"]
    for zone in base.zones:
        if zone not in built:
            continue
        try:
            zones[zone]["champion"] = forecast_zone_task(
                zone, day, champion.model, champion.alias, champion.role
            )
            # Preserve the original top-level zone summary consumed by existing operators.
            zones[zone].update(zones[zone]["champion"])
        except Exception as exc:
            errors[zone] = _short(exc)

    # The champion's TimesFM checkpoint stays cached in this process; release it before a
    # challenger loads another one, so the two never share the VPS's memory.
    from pricefc.models.timesfm import release_backends

    release_backends()
    challenger_results: dict[str, Any] = {}
    for challenger in (item for item in base.serving.models if item.role == "challenger"):
        for zone in base.zones:
            if zone not in built:
                continue
            try:
                from pricefc.serving.registry import resolve_optional

                version = resolve_optional(
                    zone, challenger.alias, tracking_uri=base.mlflow.tracking_uri
                )
                if version is None:
                    log.info(
                        "challenger_alias_missing",
                        zone=zone,
                        alias=challenger.alias,
                        model=challenger.model,
                    )
                    continue
                result = forecast_zone_task(
                    zone,
                    day,
                    challenger.model,
                    challenger.alias,
                    challenger.role,
                )
                challenger_results[f"{zone}:{challenger.alias}"] = result
            except Exception as exc:
                failure = _short(exc)
                challenger_results[f"{zone}:{challenger.alias}"] = {"error": failure}
                _alert_challenger_failure(zone, day, challenger.alias, exc)
    out = {
        "day": day.isoformat(),
        "ingest": ingest,
        "silver": silver,
        "zones": zones,
        "challengers": challenger_results,
        "errors": errors,
    }
    if errors:
        raise RuntimeError(f"forecast-daily {day}: {errors}")
    print(f"forecast ok: {out}")
    return out


def deployments(env: str) -> list[Any]:
    """Scheduled deployments for one environment (cron in Europe/Stockholm)."""
    from prefect.schedules import Cron

    tz = "Europe/Stockholm"
    # Staging runs ahead of production so the two never compete for memory.
    morning = os.environ.get("PRICEFC_CRON_MORNING", "15 8 * * *")
    afternoon = os.environ.get("PRICEFC_CRON_AFTERNOON", "30 13 * * *")
    # After the 09:00 origin, so the live inputs are exactly what the origin allows.
    forecast = os.environ.get("PRICEFC_CRON_FORECAST", "5 9 * * *")
    scoring = os.environ.get("PRICEFC_CRON_SCORE", "0 14,17 * * *")
    return [
        *(
            ingest_daily.to_deployment(
                name=f"{env}-{slot}", schedule=Cron(cron, timezone=tz), tags=[env]
            )
            for slot, cron in (("morning", morning), ("afternoon", afternoon))
        ),
        forecast_daily.to_deployment(
            name=f"{env}-forecast", schedule=Cron(forecast, timezone=tz), tags=[env]
        ),
        score_daily.to_deployment(
            name=f"{env}-score", schedule=Cron(scoring, timezone=tz), tags=[env]
        ),
    ]


def serve() -> None:
    """Register the deployments and run them (blocking)."""
    from prefect import serve as prefect_serve

    prefect_serve(*deployments(os.environ.get("PRICEFC_ENV", "dev")))
