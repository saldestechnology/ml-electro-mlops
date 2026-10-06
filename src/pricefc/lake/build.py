"""Build the silver layer from valid raw snapshots.

Regular time series collapse to the latest valid row per natural key. Open-Meteo's live
``forecast`` endpoint is different: the same valid time has a different meaning at each pull
time, so each snapshot is retained as a separate vintage.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from pricefc.config import BaseConfig
from pricefc.ingest.snapshot import Snapshot, list_snapshots, load_latest_with_lineage
from pricefc.lineage import git_info

_SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")
_APPEND_DATASETS = {("open_meteo", "forecast")}
_EVENT_KEY_COLUMNS = ("unit_name", "unit", "name", "mrid", "event_id")


def _root(base: BaseConfig) -> Path:
    return base.paths.data_root / "lake" / "silver"


def _table_name(source: str, dataset: str, key: str) -> str:
    if dataset == "day_ahead_prices":
        if source == "elprisetjustnu":
            return f"prices_{key.lower()}"
        return f"prices_{key.lower()}_{source.lower()}"
    if source == "open_meteo":
        endpoint = dataset.split("__", maxsplit=1)[0]
        return f"weather_{endpoint}_{key.lower()}"
    return "_".join((source, dataset, key)).lower()


def _safe_path(parts: tuple[str, ...]) -> None:
    for part in parts:
        if not _SAFE.fullmatch(part):
            raise ValueError(f"unsafe silver path component: {part!r}")


def _natural_key(dataset: str, columns: list[str]) -> tuple[str, ...]:
    if "timestamp" not in columns:
        raise ValueError(f"silver input for {dataset!r} has no timestamp column")
    if dataset == "generation_unavailability":
        identifiers = [column for column in _EVENT_KEY_COLUMNS if column in columns]
        if identifiers:
            return ("timestamp", *identifiers)
    return ("timestamp",)


def _append_style(source: str, dataset: str) -> bool:
    endpoint = dataset.split("__", maxsplit=1)[0]
    return (source, endpoint) in _APPEND_DATASETS


def _excluded_known_bad_days(snapshots: list[Snapshot]) -> set[str]:
    """Read the per-pull exclusion record written by the price ingester."""
    days: set[str] = set()
    for snapshot in snapshots:
        chunks = snapshot.manifest.get("chunks", [])
        if not isinstance(chunks, list):
            continue
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            excluded = chunk.get("excluded_known_bad_days", {})
            if isinstance(excluded, (dict, list)):
                days.update(str(day) for day in excluded)
    return days


def _drop_known_bad_price_days(
    frame: pd.DataFrame, source: str, dataset: str, days: set[str], timezone: str
) -> pd.DataFrame:
    if source != "elprisetjustnu" or dataset != "day_ahead_prices" or not days or frame.empty:
        return frame
    local_days = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(timezone).dt.date
    return frame.loc[~local_days.astype(str).isin(days)].reset_index(drop=True)


def _json_value(value: Any) -> str:
    if pd.isna(value):
        return "null"
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (pd.Timestamp, datetime)):
        value = value.isoformat()
    return json.dumps(value, sort_keys=True, default=str)


def _conflict_frame(
    raw_root: Path,
    snapshots: list[Snapshot],
    key_cols: tuple[str, ...],
    excluded_days: set[str],
    source: str,
    dataset: str,
    key: str,
    timezone: str,
    append_style: bool,
) -> pd.DataFrame:
    columns = [
        *key_cols,
        "column",
        "old",
        "new",
        "old_pulled_at",
        "new_pulled_at",
        "old_snapshot",
        "new_snapshot",
    ]
    if append_style:
        return pd.DataFrame(columns=columns)

    rows = []
    for snapshot in snapshots:
        frame = snapshot.read()
        frame = _drop_known_bad_price_days(frame, source, dataset, excluded_days, timezone)
        if frame.empty:
            continue
        # Match load_latest's within-snapshot last-row behavior for unusual inputs with duplicate
        # natural keys. Snapshot validation normally prevents this for series tables.
        frame = frame.drop_duplicates(list(key_cols), keep="last").copy()
        frame["_pulled_at"] = pd.Timestamp(snapshot.pulled_at)
        frame["_snapshot"] = snapshot.path.relative_to(raw_root).as_posix()
        rows.append(frame)
    if not rows:
        return pd.DataFrame(columns=columns)

    combined = pd.concat(rows, ignore_index=True, sort=False)
    combined = combined.sort_values([*key_cols, "_pulled_at"], kind="stable")
    excluded_cols = {*key_cols, "_pulled_at", "_snapshot"}
    value_cols = [column for column in combined.columns if column not in excluded_cols]
    overlaps = combined.loc[combined.duplicated(list(key_cols), keep=False)].copy()
    if overlaps.empty:
        return pd.DataFrame(columns=columns)

    grouped = overlaps.groupby(list(key_cols), dropna=False, sort=False)
    prior_pulled_at = grouped["_pulled_at"].shift()
    prior_snapshot = grouped["_snapshot"].shift()
    has_prior = prior_pulled_at.notna()
    records: list[dict[str, Any]] = []
    key_frame = overlaps.loc[:, list(key_cols)]
    for column in value_cols:
        previous = overlaps.groupby(list(key_cols), dropna=False, sort=False)[column].shift()
        current = overlaps[column]
        same = current.eq(previous) | (current.isna() & previous.isna())
        conflict_indices = overlaps.index[has_prior & ~same]
        for index in conflict_indices:
            record = key_frame.loc[index].to_dict()
            record.update(
                {
                    "column": column,
                    "old": _json_value(previous.loc[index]),
                    "new": _json_value(current.loc[index]),
                    "old_pulled_at": prior_pulled_at.loc[index],
                    "new_pulled_at": overlaps.at[index, "_pulled_at"],
                    "old_snapshot": prior_snapshot.loc[index],
                    "new_snapshot": overlaps.at[index, "_snapshot"],
                }
            )
            records.append(record)
    return pd.DataFrame.from_records(records, columns=columns)


def _append_rows(
    raw_root: Path,
    snapshots: list[Snapshot],
    excluded_days: set[str],
    source: str,
    dataset: str,
    key: str,
    timezone: str,
) -> pd.DataFrame:
    frames = []
    for snapshot in snapshots:
        frame = snapshot.read()
        frame = _drop_known_bad_price_days(frame, source, dataset, excluded_days, timezone)
        frame = frame.copy()
        frame["_pulled_at"] = pd.Timestamp(snapshot.pulled_at)
        frame["_snapshot"] = snapshot.path.relative_to(raw_root).as_posix()
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True, sort=False)
    return combined.sort_values(["timestamp", "_pulled_at"], kind="stable").reset_index(drop=True)


def _snapshot_list(raw_root: Path, snapshots: list[Snapshot]) -> list[dict[str, str]]:
    return [
        {
            "path": snapshot.path.relative_to(raw_root).as_posix(),
            "pulled_at": snapshot.pulled_at.isoformat(),
            "data_sha256": str(snapshot.manifest["data_sha256"]),
        }
        for snapshot in snapshots
    ]


def _input_signature(snapshots: list[Snapshot], append_style: bool) -> dict[str, Any]:
    hashes = sorted({str(snapshot.manifest["data_sha256"]) for snapshot in snapshots})
    signature: dict[str, Any] = {"data_sha256_set": hashes}
    if append_style:
        # Pull time is part of the identity for issued forecast vintages, even when two pulls
        # happen to contain byte-for-byte equivalent values.
        signature["vintages"] = [
            [str(snapshot.manifest["data_sha256"]), snapshot.pulled_at.isoformat()]
            for snapshot in sorted(snapshots, key=lambda item: item.pulled_at)
        ]
    return signature


def _write_table(table_path: Path, frame: pd.DataFrame, conflicts: pd.DataFrame) -> None:
    table_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = table_path.with_name(f".{table_path.name}.tmp-{uuid.uuid4().hex}")
    backup = table_path.with_name(f".{table_path.name}.old-{uuid.uuid4().hex}")
    temporary.mkdir()
    try:
        if not frame.empty:
            years = pd.to_datetime(frame["timestamp"], utc=True).dt.year
            for year, partition in frame.groupby(years, sort=True):
                year_dir = temporary / f"year={year}"
                year_dir.mkdir()
                partition.to_parquet(year_dir / "part-0.parquet", index=False)
        else:
            frame.to_parquet(temporary / "_empty.parquet", index=False)
        conflicts.to_parquet(temporary / "_conflicts.parquet", index=False)

        had_previous = table_path.exists()
        if had_previous:
            os.replace(table_path, backup)
        try:
            os.replace(temporary, table_path)
        except Exception:
            if had_previous and backup.exists():
                os.replace(backup, table_path)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
        if backup.exists() and table_path.exists():
            shutil.rmtree(backup)


def _time_range(frame: pd.DataFrame) -> dict[str, str | None]:
    if frame.empty:
        return {"start": None, "end": None}
    timestamps = pd.to_datetime(frame["timestamp"], utc=True)
    return {"start": timestamps.min().isoformat(), "end": timestamps.max().isoformat()}


def build_silver(base: BaseConfig, sources: list[str] | None = None) -> dict[str, Any]:
    """Build or incrementally refresh ``data/lake/silver`` from valid raw snapshots.

    Args:
        base: Resolved project configuration. The lake lives under ``paths.data_root/lake``.
        sources: Optional source names (for example ``["open_meteo"]``).

    Returns:
        A compact report containing built, skipped and empty input tables.
    """
    raw_root = base.paths.raw
    silver_root = _root(base)
    manifest_path = silver_root / "_manifest.json"
    old_manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    old_tables = old_manifest.get("tables", {})
    all_groups: dict[tuple[str, str, str], Path] = {}
    for manifest_file in raw_root.glob("*/*/*/pulled_at=*/manifest.json"):
        data = json.loads(manifest_file.read_text())
        source, dataset, key = str(data["source"]), str(data["dataset"]), str(data["key"])
        if sources is None or source in sources:
            all_groups[(source, dataset, key)] = manifest_file

    name_counts: dict[str, int] = {}
    for source, dataset, key in all_groups:
        name = _table_name(source, dataset, key)
        name_counts[name] = name_counts.get(name, 0) + 1

    built_at = datetime.now(UTC)
    git_sha, _ = git_info()
    report: dict[str, Any] = {"built": [], "skipped": [], "empty": [], "rows": 0, "conflicts": 0}
    next_tables = dict(old_tables)
    for source, dataset, key in sorted(all_groups):
        _safe_path((source, dataset, key))
        table_name = _table_name(source, dataset, key)
        if name_counts[table_name] > 1:
            table_name = f"{table_name}_{dataset.replace('__', '_').lower()}"
        _safe_path((table_name,))
        snapshots = list_snapshots(raw_root, source, dataset, key)
        if not snapshots:
            report["empty"].append({"source": source, "dataset": dataset, "key": key})
            continue

        append_style = _append_style(source, dataset)
        signature = _input_signature(snapshots, append_style)
        previous = old_tables.get(table_name, {})
        table_path = silver_root / source / dataset / key
        if previous.get("input_signature") == signature and table_path.exists():
            report["skipped"].append(table_name)
            continue

        excluded_days = _excluded_known_bad_days(snapshots)
        key_cols: tuple[str, ...]
        if append_style:
            frame = _append_rows(
                raw_root,
                snapshots,
                excluded_days,
                source,
                dataset,
                key,
                base.timezone,
            )
            key_cols = ("timestamp",)
        else:
            first = snapshots[0].read()
            key_cols = _natural_key(dataset, list(first.columns))
            frame, _ = load_latest_with_lineage(
                raw_root,
                source,
                dataset,
                key,
                key_cols=key_cols,
                valid_snapshots=snapshots,
            )
            frame = _drop_known_bad_price_days(frame, source, dataset, excluded_days, base.timezone)
        conflicts = _conflict_frame(
            raw_root,
            snapshots,
            key_cols,
            excluded_days,
            source,
            dataset,
            key,
            base.timezone,
            append_style,
        )
        _write_table(table_path, frame, conflicts)
        table_info = {
            "table_name": table_name,
            "source": source,
            "dataset": dataset,
            "key": key,
            "rows": len(frame),
            "time_range": _time_range(frame),
            "source_snapshots": _snapshot_list(raw_root, snapshots),
            "input_signature": signature,
            "conflicts": len(conflicts),
            "built_at": built_at.isoformat(),
            "git_sha": git_sha,
            "collapse": "append_by_pulled_at" if append_style else "latest_by_natural_key",
            "natural_key": list(key_cols) + (["_pulled_at"] if append_style else []),
            "known_bad_days_excluded": sorted(excluded_days),
        }
        next_tables[table_name] = table_info
        report["built"].append(table_name)
        report["rows"] += len(frame)
        report["conflicts"] += len(conflicts)

    if all_groups:
        silver_root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "built_at": built_at.isoformat(),
            "git_sha": git_sha,
            "tables": next_tables,
        }
        temporary_manifest = manifest_path.with_name(f"._manifest.tmp-{uuid.uuid4().hex}")
        temporary_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        os.replace(temporary_manifest, manifest_path)
    return report
