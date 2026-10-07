"""Score live daily forecasts against published raw day-ahead prices."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import structlog

from pricefc.backtest.metrics import pinball, qcol
from pricefc.config import BaseConfig, load_features_config, load_ingest_config
from pricefc.datasets.loaders import load_hourly_prices
from pricefc.ingest.snapshot import list_snapshots
from pricefc.timeutils import local_day_bounds_utc

log = structlog.get_logger(__name__)

LOCAL_TZ = ZoneInfo("Europe/Stockholm")
FEATURES_CONFIG = Path("configs/features.yaml")
INGEST_CONFIG = Path("configs/ingest.yaml")


@dataclass(frozen=True)
class ActualIndex:
    """Actual prices keyed by UTC instant and Stockholm wall-clock hour."""

    by_utc_ns: dict[int, float]
    by_local_hour: dict[tuple[date, int], list[tuple[int, float]]]


@dataclass(frozen=True)
class DailyScore:
    zone: str
    role: str
    origin_date: date
    target_date: date
    scored_at: datetime
    n_hours: int
    model_version: str
    pinball: float
    naive_7d_pinball: float | None
    skill: float | None
    coverage_50: float
    coverage_90: float
    mae_median: float


# Snapshot directories are immutable; their paths identify the exact input set. Keep one entry
# per root/zone/input set and discard superseded entries to bound process memory.
_ACTUALS_CACHE: dict[tuple[str, str, str, tuple[str, ...]], ActualIndex] = {}


def timestamp_utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def naive_value(stamp: pd.Timestamp, actuals: ActualIndex | None) -> float | None:
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


def enrich(frame: pd.DataFrame, actuals: ActualIndex | None) -> pd.DataFrame:
    """Attach actual and 7-day local-hour actual columns to forecast rows."""
    out = frame.copy()
    actual_values: list[float | None] = []
    naive_values: list[float | None] = []
    for value in out["target_time"]:
        stamp = timestamp_utc(value)
        actual_values.append(actuals.by_utc_ns.get(stamp.value) if actuals is not None else None)
        naive_values.append(naive_value(stamp, actuals))
    out["actual"] = actual_values
    out["naive_7d"] = naive_values
    return out


def pinball_mean(
    frame: pd.DataFrame, quantiles: Sequence[float], mask: pd.Series, prediction: str | None = None
) -> float | None:
    """Average elementwise pinball loss across scored hours and quantiles."""
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


def score(frame: pd.DataFrame, quantiles: Sequence[float]) -> dict[str, float | None] | None:
    """Score rows with actuals; compare model and naive only on paired hours."""
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
    live_pinball = pinball_mean(frame, quantiles, forecast_mask)
    naive_pinball = pinball_mean(frame, quantiles, naive_mask, "naive_7d")
    paired_pinball = pinball_mean(frame, quantiles, naive_mask)
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


def load_actuals(base: BaseConfig, zone: str) -> ActualIndex | None:
    """Load dataset-equivalent hourly prices from valid raw snapshots, cached by snapshot IDs."""
    features = load_features_config(FEATURES_CONFIG)
    ingest = load_ingest_config(INGEST_CONFIG)
    source = ingest.prices.source
    snapshots = list_snapshots(base.paths.raw, source, "day_ahead_prices", zone)
    if not snapshots:
        return None

    snapshot_ids = tuple(
        f"{snapshot.path.resolve()}:{snapshot.manifest.get('content_sha256', '')}"
        for snapshot in snapshots
    )
    root = str(base.paths.data_root.resolve())
    cache_key = (root, zone, source, snapshot_ids)
    cached = _ACTUALS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    prices, _ = load_hourly_prices(base.paths.raw, source, zone, base, features.dataset, "price")
    by_utc_ns: dict[int, float] = {}
    by_local_hour: dict[tuple[date, int], list[tuple[int, float]]] = {}
    for stamp, actual in prices.data["price"].items():
        if pd.isna(actual) or not math.isfinite(float(actual)):
            continue
        utc_stamp = timestamp_utc(stamp)
        local = utc_stamp.tz_convert(LOCAL_TZ)
        number = float(actual)
        by_utc_ns[utc_stamp.value] = number
        utc_offset = local.utcoffset()
        offset = int(utc_offset.total_seconds()) if utc_offset is not None else 0
        by_local_hour.setdefault((local.date(), local.hour), []).append((offset, number))

    indexed = ActualIndex(by_utc_ns, by_local_hour)
    for key in [key for key in _ACTUALS_CACHE if key[:3] == (root, zone, source)]:
        del _ACTUALS_CACHE[key]
    _ACTUALS_CACHE[cache_key] = indexed
    return indexed


def _target_hours(day: date, timezone: str) -> pd.DatetimeIndex:
    start, end = local_day_bounds_utc(day, timezone)
    return pd.date_range(start, end, freq="h", inclusive="left", name="target_time")


def _model_version(frame: pd.DataFrame) -> str:
    if "model_version" not in frame:
        return "unknown"
    values = frame["model_version"].dropna()
    return str(values.iloc[0]) if not values.empty else "unknown"


def _daily_score(
    frame: pd.DataFrame,
    actuals: ActualIndex,
    base: BaseConfig,
    zone: str,
    role: str,
    origin: date,
) -> DailyScore | None:
    target_date = origin + timedelta(days=1)
    expected = _target_hours(target_date, base.timezone)
    if frame.empty or "target_time" not in frame:
        return None
    target_times = pd.to_datetime(frame["target_time"], utc=True, errors="coerce")
    if target_times.isna().any() or len(frame) != len(expected):
        return None
    indexed_times = pd.DatetimeIndex(target_times, name="target_time").sort_values()
    if not indexed_times.equals(expected):
        return None

    quantile_cols = [qcol(q) for q in base.quantiles]
    if any(column not in frame for column in quantile_cols):
        return None
    out = frame.copy()
    out["target_time"] = target_times
    out = out.sort_values("target_time").reset_index(drop=True)
    quantile_values = out[quantile_cols].apply(pd.to_numeric, errors="coerce")
    values = quantile_values.to_numpy(dtype="float64")
    if quantile_values.isna().any().any() or not np.isfinite(values).all():
        return None
    out[quantile_cols] = quantile_values
    scored = score(enrich(out, actuals), base.quantiles)
    if scored is None or any(actuals.by_utc_ns.get(stamp.value) is None for stamp in expected):
        return None
    pinball_value = scored["pinball"]
    coverage_50 = scored["coverage_50"]
    coverage_90 = scored["coverage_90"]
    mae_median = scored["mae_median"]
    if pinball_value is None or coverage_50 is None or coverage_90 is None or mae_median is None:
        return None

    return DailyScore(
        zone=zone,
        role=role,
        origin_date=origin,
        target_date=target_date,
        scored_at=datetime.now(UTC),
        n_hours=len(expected),
        model_version=_model_version(out),
        pinball=float(pinball_value),
        naive_7d_pinball=scored["naive_7d_pinball"],
        skill=scored["skill"],
        coverage_50=float(coverage_50),
        coverage_90=float(coverage_90),
        mae_median=float(mae_median),
    )


def _score_path(base: BaseConfig, zone: str, role: str) -> Path:
    from pricefc.serving.live import forecast_path

    return forecast_path(base, zone, date(2000, 1, 1), role).parent


def _write_scores(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    frame.to_parquet(tmp, index=False)
    tmp.replace(path)


def _log_score(base: BaseConfig, item: DailyScore) -> None:
    import mlflow

    from pricefc.tracking.mlflow_utils import base_tags, start_run

    tags = base_tags(base, zone=item.zone, pipeline_stage="score")
    tags.update(
        {
            "origin_date": item.origin_date.isoformat(),
            "target_date": item.target_date.isoformat(),
            "role": item.role,
            "model_version": item.model_version,
        }
    )
    metrics = {
        "pinball": item.pinball,
        "naive_7d_pinball": item.naive_7d_pinball,
        "skill": item.skill,
        "coverage_50": item.coverage_50,
        "coverage_90": item.coverage_90,
        "mae_median": item.mae_median,
        "n_hours": item.n_hours,
    }
    with start_run(
        "forecast-score",
        tags,
        base,
        run_name=f"{item.zone}-{item.origin_date}-{item.role}",
    ):
        mlflow.log_metrics({key: value for key, value in metrics.items() if value is not None})


ScoreLogger = Callable[[BaseConfig, DailyScore], None]


def score_forecasts(
    base: BaseConfig,
    zones: Sequence[str] | None = None,
    roles: Sequence[str] = ("champion", "challenger"),
    days_back: int = 14,
    force: bool = False,
    *,
    log_to_mlflow: bool = True,
    mlflow_logger: ScoreLogger | None = None,
) -> list[DailyScore]:
    """Score fully published recent forecasts and upsert their durable daily records."""
    if days_back < 1:
        raise ValueError("days_back must be at least 1")
    if any(role not in ("champion", "challenger") for role in roles):
        raise ValueError(f"unknown forecast roles: {roles}")
    today = datetime.now(ZoneInfo(base.timezone)).date()
    first_day = today - timedelta(days=days_back - 1)
    selected_zones = base.zones if zones is None else zones
    newly_scored: list[DailyScore] = []

    for zone in selected_zones:
        actuals = load_actuals(base, zone)
        if actuals is None:
            continue
        for role in roles:
            root = _score_path(base, zone, role)
            if not root.is_dir():
                continue
            candidates: list[tuple[date, Path]] = []
            for path in root.glob("*.parquet"):
                try:
                    origin = date.fromisoformat(path.stem)
                except ValueError:
                    log.warning("invalid_forecast_filename", path=str(path))
                    continue
                if first_day <= origin <= today:
                    candidates.append((origin, path))

            store = base.paths.data_root / "scores" / zone / f"{role}.parquet"
            existing = pd.read_parquet(store) if store.is_file() else pd.DataFrame()
            if not existing.empty:
                existing["origin_date"] = existing["origin_date"].map(
                    lambda value: pd.Timestamp(value).date().isoformat()
                )
            existing_origins = set(existing.get("origin_date", pd.Series(dtype="string")))
            additions: list[DailyScore] = []
            for origin, path in sorted(candidates):
                if not force and origin.isoformat() in existing_origins:
                    continue
                frame = pd.read_parquet(path)
                item = _daily_score(frame, actuals, base, zone, role, origin)
                if item is not None:
                    additions.append(item)

            if not additions:
                continue
            new_rows = pd.DataFrame(asdict(item) for item in additions)
            if not existing.empty:
                replaced_origins = {item.origin_date.isoformat() for item in additions}
                existing = existing[~existing["origin_date"].isin(replaced_origins)]
            combined = pd.concat([existing, new_rows], ignore_index=True)
            combined = combined.drop_duplicates(subset=["origin_date"], keep="last")
            combined = combined.sort_values("origin_date").reset_index(drop=True)
            _write_scores(combined, store)
            newly_scored.extend(additions)

    if log_to_mlflow:
        logger = mlflow_logger or _log_score
        for item in newly_scored:
            try:
                logger(base, item)
            except Exception as exc:
                log.warning(
                    "forecast_score_mlflow_failed",
                    zone=item.zone,
                    role=item.role,
                    origin_date=item.origin_date.isoformat(),
                    error=str(exc),
                )
    return newly_scored
