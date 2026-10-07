"""Read forecast and state files for the read-only web dashboard."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import structlog

from pricefc.backtest.metrics import qcol
from pricefc.config import BaseConfig
from pricefc.serving import score as scoring

log = structlog.get_logger(__name__)

ZONES = ("SE1", "SE2", "SE3", "SE4")
MODEL_NAME = "ensemble_hourly_exp"
LOCAL_TZ = ZoneInfo("Europe/Stockholm")


@dataclass(frozen=True)
class _ForecastFile:
    origin: date
    path: Path


def _forecast_files(base: BaseConfig, zone: str, role: str = "champion") -> list[_ForecastFile]:
    """List forecast files with valid ISO origin names, newest first."""
    root = base.paths.data_root / "forecasts" / zone
    if role == "challenger":
        root /= "challenger"
    elif role != "champion":
        raise ValueError(f"unknown forecast role {role!r}")
    files: list[_ForecastFile] = []
    for path in root.glob("*.parquet"):
        try:
            origin = date.fromisoformat(path.stem)
        except ValueError:
            log.warning("invalid_forecast_filename", path=str(path))
            continue
        files.append(_ForecastFile(origin, path))
    return sorted(files, key=lambda item: item.origin, reverse=True)


def _read_state_metadata(base: BaseConfig, zone: str) -> dict[str, Any]:
    """Read the small JSON metadata file and never load the adjacent pickle."""
    path = base.paths.data_root / "state" / zone / MODEL_NAME / "state.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("invalid_state_metadata", path=str(path), error=str(exc))
        return {}
    return value if isinstance(value, dict) else {}


_ActualIndex = scoring.ActualIndex
_actual_index = scoring.load_actuals
_enrich = scoring.enrich
_naive_value = scoring.naive_value
_pinball_mean = scoring.pinball_mean
_score = scoring.score
_timestamp_utc = scoring.timestamp_utc


def _timestamp_local_iso(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return _timestamp_utc(value).tz_convert(LOCAL_TZ).isoformat()


def _number(row: pd.Series, column: str) -> float | None:
    value = row.get(column)
    if value is None or pd.isna(value):
        return None
    return float(value)


def forecast_origins(base: BaseConfig, zone: str, role: str = "champion") -> list[str]:
    """Return available origins newest first, using the forecast filenames as the index."""
    return [item.origin.isoformat() for item in _forecast_files(base, zone, role)]


def zone_summaries(base: BaseConfig, today: date | None = None) -> list[dict[str, Any]]:
    """Return the newest served forecast and readable state metadata for all zones."""
    today = today or datetime.now(LOCAL_TZ).date()
    summaries: list[dict[str, Any]] = []
    for zone in ZONES:
        files = _forecast_files(base, zone)
        challenger_files = _forecast_files(base, zone, "challenger")
        latest = files[0] if files else None
        row: pd.Series | None = None
        if latest is not None:
            frame = pd.read_parquet(latest.path)
            if not frame.empty:
                row = frame.iloc[0]
        metadata = _read_state_metadata(base, zone)
        origin = latest.origin if latest is not None else None
        target_date = str(row["target_date"]) if row is not None and "target_date" in row else None
        model_version = (
            str(row["model_version"])
            if row is not None and pd.notna(row.get("model_version"))
            else None
        )
        made_at = _timestamp_local_iso(row.get("forecast_made_at")) if row is not None else None
        summaries.append(
            {
                "zone": zone,
                "latest_origin": origin.isoformat() if origin else None,
                "target_date": target_date,
                "model_version": model_version,
                "forecast_made_at": made_at,
                "last_fit": metadata.get("last_fit"),
                "stale": origin is None or origin < today,
                "challenger": _challenger_summary(challenger_files),
            }
        )
    return summaries


def _challenger_summary(files: list[_ForecastFile]) -> dict[str, Any] | None:
    if not files:
        return None
    latest = files[0]
    frame = pd.read_parquet(latest.path)
    if frame.empty:
        return None
    version = frame.iloc[0].get("model_version")
    return {
        "latest_origin": latest.origin.isoformat(),
        "model_version": _number_or_string(version),
    }


def forecast_data(
    base: BaseConfig, zone: str, origin: date | None = None, role: str = "champion"
) -> dict[str, Any] | None:
    """Build an hourly forecast response, joining only published actuals and naive prices."""
    files = _forecast_files(base, zone, role)
    selected = (
        next((item for item in files if item.origin == origin), None)
        if origin
        else (files[0] if files else None)
    )
    if selected is None:
        return None

    frame = pd.read_parquet(selected.path).sort_values("target_time").reset_index(drop=True)
    if frame.empty:
        return None
    actuals = _actual_index(base, zone)
    hours: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        stamp = _timestamp_utc(row["target_time"])
        local = stamp.tz_convert(LOCAL_TZ)
        actual = actuals.by_utc_ns.get(stamp.value) if actuals is not None else None
        hours.append(
            {
                "target_time": stamp.tz_convert(LOCAL_TZ).isoformat(),
                "hour_local": local.hour,
                **{column: _number(row, column) for column in (qcol(q) for q in base.quantiles)},
                "actual": actual,
                "naive_7d": _naive_value(stamp, actuals),
            }
        )

    first = frame.iloc[0]
    made_at = _timestamp_local_iso(first.get("forecast_made_at"))
    origin_value = first.get("origin")
    return {
        "zone": zone,
        "origin_date": selected.origin.isoformat(),
        "origin": _timestamp_local_iso(origin_value),
        "target_date": str(first.get("target_date", "")),
        "model": str(first.get("model", MODEL_NAME)),
        "role": role,
        "model_version": _number_or_string(first.get("model_version")),
        "forecast_made_at": made_at,
        "quantiles": list(base.quantiles),
        "hours": hours,
    }


def _number_or_string(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return str(value)


def performance_data(
    base: BaseConfig,
    zone: str,
    days: int,
    backtest: dict[str, float],
    today: date | None = None,
    role: str = "champion",
) -> dict[str, Any]:
    """Score forecasts in the latest `days` calendar-day window.

    Champion scores include every hour with an actual and complete quantiles. Naive 7-day
    scores include only hours with both an actual and a same-local-hour value from one week
    earlier, because the naive point forecast is applied at every quantile.
    """
    today = today or datetime.now(LOCAL_TZ).date()
    first_day = today - timedelta(days=days - 1)
    selected = [
        item for item in _forecast_files(base, zone, role) if first_day <= item.origin <= today
    ]
    actuals = _actual_index(base, zone)
    parts: list[pd.DataFrame] = []
    for item in reversed(selected):
        frame = pd.read_parquet(item.path)
        if frame.empty:
            continue
        frame = frame.copy()
        frame["origin_date"] = item.origin.isoformat()
        parts.append(_enrich(frame, actuals))

    scored: list[dict[str, Any]] = []
    all_rows = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    live = _score(all_rows, base.quantiles) if not all_rows.empty else None
    if not all_rows.empty:
        for origin_date, frame in all_rows.groupby("origin_date", sort=True):
            result = _score(frame, base.quantiles)
            if result is not None:
                scored.append(
                    {
                        "origin_date": str(origin_date),
                        "pinball": result["pinball"],
                        "naive_7d_pinball": result["naive_7d_pinball"],
                        "coverage_90": result["coverage_90"],
                    }
                )
    return {
        "zone": zone,
        "role": role,
        "days": days,
        "n_origins_scored": len(scored),
        "live": live,
        "daily": scored,
        "backtest": backtest,
    }


def model_metadata(base: BaseConfig, zone: str) -> dict[str, Any]:
    """Return readable served-state JSON and backup directory names without opening a pickle."""
    metadata = _read_state_metadata(base, zone)
    backups_root = base.paths.data_root / "state" / zone / f"{MODEL_NAME}.backups"
    backups: list[str] = []
    if backups_root.is_dir():
        for path in backups_root.iterdir():
            if path.is_dir():
                try:
                    backups.append(date.fromisoformat(path.name).isoformat())
                except ValueError:
                    continue
    backups.sort(reverse=True)
    datasets = metadata.get("datasets", {})
    return {
        "zone": zone,
        "spec": metadata.get("spec", MODEL_NAME),
        "model_version": metadata.get("source_version"),
        "first_origin": metadata.get("first_origin"),
        "last_origin": metadata.get("last_origin"),
        "last_fit": metadata.get("last_fit"),
        "origins_served": metadata.get("origins_served", 0),
        "train_start": metadata.get("train_start"),
        "datasets": {
            "train": datasets.get("train"),
            "eval": datasets.get("eval"),
            "live": datasets.get("live"),
        },
        "versions": metadata.get("versions", {}),
        "git_sha": metadata.get("git_sha", "unknown"),
        "saved_at": _timestamp_local_iso(metadata.get("saved_at")),
        "backups": backups,
    }
