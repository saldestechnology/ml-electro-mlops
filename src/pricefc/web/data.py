"""Read forecast and state files for the read-only web dashboard."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import structlog

from pricefc.backtest import run as bt_run
from pricefc.backtest.metrics import pinball, qcol
from pricefc.config import BaseConfig

log = structlog.get_logger(__name__)

ZONES = ("SE1", "SE2", "SE3", "SE4")
MODEL_NAME = "ensemble_hourly_exp"
LOCAL_TZ = ZoneInfo("Europe/Stockholm")


@dataclass(frozen=True)
class _ActualIndex:
    by_utc_ns: dict[int, float]
    by_local_hour: dict[tuple[date, int], list[tuple[int, float]]]


# Include the root so separate configs with the same immutable version name cannot collide.
_ACTUALS_CACHE: dict[tuple[str, str, str], _ActualIndex] = {}


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


def _actual_index(base: BaseConfig, zone: str) -> _ActualIndex | None:
    """Read and index actuals once for each zone and true-lead dataset version."""
    try:
        dataset = bt_run.latest_dataset(base, zone, "true_lead")
    except FileNotFoundError:
        return None

    cache_key = (str(base.paths.data_root.resolve()), zone, dataset.version)
    cached = _ACTUALS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        frame = dataset.read()
    except FileNotFoundError:
        return None
    if "target_time" not in frame or "y" not in frame:
        log.warning("actuals_missing_columns", zone=zone, version=dataset.version)
        return None

    target_times = pd.to_datetime(frame["target_time"], utc=True, errors="coerce")
    actuals = pd.to_numeric(frame["y"], errors="coerce")
    by_utc_ns: dict[int, float] = {}
    by_local_hour: dict[tuple[date, int], list[tuple[int, float]]] = {}
    for stamp, actual in zip(target_times, actuals, strict=False):
        if pd.isna(stamp) or pd.isna(actual) or not math.isfinite(float(actual)):
            continue
        local = pd.Timestamp(stamp).tz_convert(LOCAL_TZ)
        number = float(actual)
        by_utc_ns[pd.Timestamp(stamp).value] = number
        utc_offset = local.utcoffset()
        offset = int(utc_offset.total_seconds()) if utc_offset is not None else 0
        by_local_hour.setdefault((local.date(), local.hour), []).append((offset, number))

    indexed = _ActualIndex(by_utc_ns, by_local_hour)
    _ACTUALS_CACHE[cache_key] = indexed
    return indexed


def _timestamp_utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def _timestamp_local_iso(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return _timestamp_utc(value).tz_convert(LOCAL_TZ).isoformat()


def _naive_value(stamp: pd.Timestamp, actuals: _ActualIndex | None) -> float | None:
    """Look up the actual for the same Stockholm wall-clock hour one week earlier."""
    if actuals is None:
        return None
    local = stamp.tz_convert(LOCAL_TZ)
    key = (local.date() - timedelta(days=7), local.hour)
    candidates = actuals.by_local_hour.get(key, [])
    if not candidates:
        return None
    utc_offset = local.utcoffset()
    offset = int(utc_offset.total_seconds()) if utc_offset is not None else 0
    return next(
        (value for candidate_offset, value in candidates if candidate_offset == offset),
        candidates[0][1],
    )


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


def _enrich(frame: pd.DataFrame, actuals: _ActualIndex | None) -> pd.DataFrame:
    """Attach actual and 7-day local-hour actual columns to forecast rows."""
    out = frame.copy()
    actual_values: list[float | None] = []
    naive_values: list[float | None] = []
    for value in out["target_time"]:
        stamp = _timestamp_utc(value)
        actual_values.append(actuals.by_utc_ns.get(stamp.value) if actuals is not None else None)
        naive_values.append(_naive_value(stamp, actuals))
    out["actual"] = actual_values
    out["naive_7d"] = naive_values
    return out


def _pinball_mean(
    frame: pd.DataFrame, quantiles: list[float], mask: pd.Series, prediction: str | None = None
) -> float | None:
    """Average the project's elementwise pinball loss across scored hours and quantiles."""
    if not mask.any():
        return None
    y = frame.loc[mask, "actual"].to_numpy(dtype="float64")
    losses: list[np.ndarray] = []
    for tau in quantiles:
        pred = (
            frame.loc[mask, prediction].to_numpy(dtype="float64")
            if prediction is not None
            else frame.loc[mask, qcol(tau)].to_numpy(dtype="float64")
        )
        losses.append(pinball(y, pred, tau))
    return float(np.concatenate(losses).mean())


def _score(frame: pd.DataFrame, quantiles: list[float]) -> dict[str, float | None] | None:
    """Score actual hours. The naive loss, and the skill, use only hours that have both an
    actual and a naive value, so model and naive are compared on the same hours."""
    quantile_cols = [qcol(tau) for tau in quantiles]
    actual = frame["actual"].notna()
    forecast_mask = actual & frame[quantile_cols].notna().all(axis=1)
    if not forecast_mask.any():
        return None
    naive_mask = forecast_mask & frame["naive_7d"].notna()
    y = frame.loc[forecast_mask, "actual"].to_numpy(dtype="float64")
    q50 = frame.loc[forecast_mask, qcol(0.5)].to_numpy(dtype="float64")
    coverage_50 = np.mean(
        (y >= frame.loc[forecast_mask, qcol(0.25)].to_numpy(dtype="float64"))
        & (y <= frame.loc[forecast_mask, qcol(0.75)].to_numpy(dtype="float64"))
    )
    coverage_90 = np.mean(
        (y >= frame.loc[forecast_mask, qcol(0.05)].to_numpy(dtype="float64"))
        & (y <= frame.loc[forecast_mask, qcol(0.95)].to_numpy(dtype="float64"))
    )
    live_pinball = _pinball_mean(frame, quantiles, forecast_mask)
    naive_pinball = _pinball_mean(frame, quantiles, naive_mask, "naive_7d")
    paired_pinball = _pinball_mean(frame, quantiles, naive_mask)
    skill = (
        1.0 - paired_pinball / naive_pinball
        if paired_pinball is not None and naive_pinball not in (None, 0.0)
        else None
    )
    return {
        "pinball": live_pinball,
        "naive_7d_pinball": naive_pinball,
        "skill": skill,
        "coverage_50": float(coverage_50),
        "coverage_90": float(coverage_90),
        "mae_median": float(np.mean(np.abs(q50 - y))),
    }


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
