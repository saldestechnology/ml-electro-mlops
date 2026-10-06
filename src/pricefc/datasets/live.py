"""Build feature rows for the current forecast origin without requiring published targets."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from pricefc.config import BaseConfig, FeaturesConfig, IngestConfig
from pricefc.datasets.build import DatasetBuilder, origin_timestamp


class LiveDataError(RuntimeError):
    """Live inputs are incomplete or do not match the stored true-lead dataset contract."""


@dataclass(frozen=True)
class LiveRows:
    zone: str
    origin_date: date
    frame: pd.DataFrame
    report: dict[str, Any]
    path: Path | None


def live_dir(base: BaseConfig, zone: str, day: date) -> Path:
    """Directory holding the replaceable live rows and manifest for an origin."""
    return base.paths.data_root / "live" / zone / day.isoformat()


def build_live_rows(
    base: BaseConfig,
    features: FeaturesConfig,
    ingest: IngestConfig,
    zone: str,
    day: date,
    *,
    write: bool = True,
) -> LiveRows:
    """Build one origin with the dataset's feature code, auditing it against available data."""
    from pricefc.backtest.run import latest_dataset

    try:
        builder = DatasetBuilder.from_snapshots(base, features, ingest, zone, "true_lead")
    except (FileNotFoundError, KeyError, ValueError) as exc:
        raise LiveDataError(
            f"cannot load required true_lead source for {zone} {day}: {exc}"
        ) from exc
    problems = builder.live_origin_problems(day)
    if problems:
        raise LiveDataError(f"live origin {zone} {day}: " + "; ".join(problems))

    frame = builder.build_origin(day)
    audit = builder.leakage_audit([day], require_target_change=False)
    try:
        eval_dataset = latest_dataset(base, zone, "true_lead")
    except FileNotFoundError as exc:
        raise LiveDataError(
            f"no latest true_lead dataset for {zone}; build the dataset before live rows"
        ) from exc
    reference = eval_dataset.read()
    _check_column_contract(frame, reference, eval_dataset.version)
    _check_missing_features(frame, reference, builder.feature_columns(frame), base.timezone)

    lineage = builder.describe()
    history_days = max(
        *features.dataset.price_lag_days,
        features.dataset.same_hour_mean_days,
        (max(features.dataset.rolling_windows_hours) + 23) // 24,
    )
    report = {
        "coverage": {
            "passed": True,
            "origin": origin_timestamp(day, base).isoformat(),
            "sources_checked": list(builder.sources),
            "own_price_days": [
                (day + timedelta(days=1 - history_days)).isoformat(),
                day.isoformat(),
            ],
            "neighbour_price_day": day.isoformat(),
            "weather_days": [day.isoformat(), (day + timedelta(days=1)).isoformat()],
        },
        "leakage_audit": audit,
        "source_lineage": lineage,
        "eval_dataset_version": eval_dataset.version,
    }

    output_path: Path | None = None
    if write:
        output_path = live_dir(base, zone, day)
        built_at = datetime.now(UTC)
        manifest = {
            "zone": zone,
            "origin_date": day.isoformat(),
            "origin": origin_timestamp(day, base).isoformat(),
            "built_at": built_at.isoformat(),
            "git_sha": os.environ.get("PRICEFC_GIT_SHA", "unknown"),
            "eval_dataset_version": eval_dataset.version,
            "rows": len(frame),
            "source_lineage": lineage,
            "audit_report": audit,
            "coverage": report["coverage"],
        }
        _write_atomically(output_path, frame, manifest)

    return LiveRows(zone, day, frame, report, output_path)


def _check_column_contract(frame: pd.DataFrame, reference: pd.DataFrame, version: str) -> None:
    expected_columns = reference.columns.tolist()
    if frame.columns.tolist() != expected_columns:
        raise LiveDataError(
            f"live rows differ from true_lead dataset {version} columns; "
            f"expected {expected_columns}, got {frame.columns.tolist()} (rebuild datasets first)"
        )
    dtype_differences = [
        f"{column}: live={frame[column].dtype}, dataset={reference[column].dtype}"
        for column in expected_columns
        if frame[column].dtype != reference[column].dtype
    ]
    if dtype_differences:
        raise LiveDataError(
            f"live rows differ from true_lead dataset {version} dtypes: "
            f"{dtype_differences[:10]} (rebuild datasets first)"
        )


def _check_missing_features(
    frame: pd.DataFrame,
    reference: pd.DataFrame,
    feature_columns: list[str],
    timezone: str,
) -> None:
    """Reject new nulls where the latest built origin has a value for that forecast hour."""
    if reference.empty or "origin_date" not in reference:
        raise LiveDataError("latest true_lead dataset has no origin rows for the feature check")
    last_day = reference["origin_date"].astype(str).max()
    latest = reference[reference["origin_date"].astype(str) == last_day]
    if "target_time" not in latest:
        raise LiveDataError("latest true_lead dataset has no target_time for the feature check")
    reference_by_hour = dict(zip(_hour_keys(latest, timezone), range(len(latest)), strict=True))
    missing: list[str] = []
    latest = latest.reset_index(drop=True)
    for live_position, key in enumerate(_hour_keys(frame, timezone)):
        reference_position = reference_by_hour.get(key)
        if reference_position is None:
            continue
        for column in feature_columns:
            if pd.notna(latest.iloc[reference_position][column]) and pd.isna(
                frame.iloc[live_position][column]
            ):
                missing.append(f"{column} at {frame.iloc[live_position]['target_time']}")
    if missing:
        raise LiveDataError(
            "live features contain new NaN values where the latest true_lead origin has "
            f"values: {missing[:10]} (rebuild or restore the missing live inputs)"
        )


def _hour_keys(frame: pd.DataFrame, timezone: str) -> list[tuple[int, int, int]]:
    local = pd.DatetimeIndex(frame["target_time"]).tz_convert(timezone)
    occurrences: dict[tuple[int, int], int] = {}
    keys = []
    for timestamp in local:
        hour = (timestamp.hour, timestamp.minute)
        occurrence = occurrences.get(hour, 0)
        occurrences[hour] = occurrence + 1
        keys.append((*hour, occurrence))
    return keys


def _write_atomically(path: Path, frame: pd.DataFrame, manifest: dict[str, Any]) -> None:
    """Stage a complete origin directory, then promote it with same-filesystem renames."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{path.name}.", dir=path.parent))
    backup: Path | None = None
    try:
        frame.to_parquet(staged / "rows.parquet", index=False)
        (staged / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")
        if path.exists():
            backup = Path(tempfile.mkdtemp(prefix=f".{path.name}.old.", dir=path.parent))
            backup.rmdir()
            path.replace(backup)
        try:
            staged.replace(path)
        except Exception:
            if backup is not None and backup.exists():
                backup.replace(path)
            raise
        if backup is not None:
            shutil.rmtree(backup)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
