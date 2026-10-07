"""Prometheus 0.0.4 exposition for the read-only web API."""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from datetime import time as local_time
from pathlib import Path
from threading import Lock
from typing import Any

import pandas as pd
import requests
import structlog

from pricefc.config import BaseConfig
from pricefc.web import data
from pricefc.web.constants import BACKTEST_BASELINES

log = structlog.get_logger(__name__)

CACHE_SECONDS = 60.0
FORECAST_CACHE_MAX = 128
FLOWS = ("ingest-daily", "forecast-daily")
_SNAKE_CASE = re.compile(r"[^a-z0-9]+")

_METRIC_DEFINITIONS: dict[str, tuple[str, str]] = {
    "pricefc_build_info": ("Build and runtime environment information.", "gauge"),
    "pricefc_forecast_last_origin_timestamp_seconds": (
        "Local 09:00 origin time of the newest forecast file.",
        "gauge",
    ),
    "pricefc_forecast_target_date_timestamp_seconds": (
        "Local midnight at the delivery date of the newest forecast file.",
        "gauge",
    ),
    "pricefc_forecast_made_at_timestamp_seconds": (
        "Unix time when the newest forecast was made.",
        "gauge",
    ),
    "pricefc_forecast_rows": ("Rows in the newest forecast file.", "gauge"),
    "pricefc_model_version": (
        "Registered model version number in the newest forecast, when known.",
        "gauge",
    ),
    "pricefc_state_last_origin_timestamp_seconds": (
        "Local 09:00 origin time last served by a persisted model state.",
        "gauge",
    ),
    "pricefc_pinball_mean": (
        "Mean pinball loss across available forecast quantiles and actual hours.",
        "gauge",
    ),
    "pricefc_naive_pinball_mean": (
        "Mean naive 7-day pinball loss on hours paired with the forecast.",
        "gauge",
    ),
    "pricefc_coverage_ratio": ("Observed coverage ratio for a forecast interval.", "gauge"),
    "pricefc_scored_days": ("Number of origins with known actuals in the window.", "gauge"),
    "pricefc_backtest_pinball": ("Stored baseline backtest mean pinball loss.", "gauge"),
    "pricefc_raw_latest_valid_pulled_at_timestamp_seconds": (
        "Unix time of the newest raw snapshot whose validation passed.",
        "gauge",
    ),
    "pricefc_raw_latest_pull_valid": (
        "Whether the newest raw snapshot pull passed validation.",
        "gauge",
    ),
    "pricefc_prefect_up": ("Whether the Prefect API is reachable and responding.", "gauge"),
    "pricefc_flow_last_success_timestamp_seconds": (
        "Unix time of the latest successful Prefect flow run.",
        "gauge",
    ),
    "pricefc_flow_last_run_timestamp_seconds": (
        "Unix time of the newest started Prefect flow run.",
        "gauge",
    ),
    "pricefc_flow_last_run_state": (
        "State type of the newest started Prefect flow run, represented by a one-hot gauge.",
        "gauge",
    ),
    "pricefc_flow_last_success_duration_seconds": (
        "Duration of the latest successful Prefect flow run in seconds.",
        "gauge",
    ),
    "pricefc_flow_runs_scheduled": (
        "Prefect scheduled flow runs in the next 48 hours.",
        "gauge",
    ),
    "pricefc_mlflow_up": ("Whether the MLflow health endpoint responds successfully.", "gauge"),
    "pricefc_metrics_render_seconds": (
        "Time spent rendering an uncached metrics response.",
        "gauge",
    ),
    "pricefc_metrics_errors": ("Errors encountered while rendering a metrics section.", "gauge"),
}

_SECTIONS = ("forecasts", "state", "quality", "raw", "prefect", "mlflow")


class _Registry:
    def __init__(self) -> None:
        self.samples: dict[str, list[tuple[dict[str, str], float]]] = {
            name: [] for name in _METRIC_DEFINITIONS
        }

    def add(self, name: str, value: float, **labels: str) -> None:
        if math.isfinite(value):
            self.samples[name].append((labels, value))

    def render(self) -> str:
        lines: list[str] = []
        for name, (help_text, metric_type) in _METRIC_DEFINITIONS.items():
            lines.append(f"# HELP {name} {_escape_help(help_text)}")
            lines.append(f"# TYPE {name} {metric_type}")
            for labels, value in self.samples[name]:
                label_text = ""
                if labels:
                    formatted = ",".join(
                        f'{key}="{_escape_label(value)}"' for key, value in labels.items()
                    )
                    label_text = "{" + formatted + "}"
                lines.append(f"{name}{label_text} {_format_number(value)}")
        return "\n".join(lines) + "\n"


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_number(value: float) -> str:
    return format(value, ".15g")


def _label(value: str) -> str:
    return _SNAKE_CASE.sub("_", value.lower()).strip("_") or "unknown"


def _timestamp_seconds(value: Any) -> float | None:
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            return None
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        return float(stamp.timestamp())
    except (OverflowError, TypeError, ValueError):
        return None


def _origin_seconds(value: Any) -> float | None:
    try:
        if isinstance(value, datetime):
            origin_day = value.date()
        elif isinstance(value, date):
            origin_day = value
        else:
            origin_day = date.fromisoformat(str(value)[:10])
        return datetime.combine(origin_day, local_time(9), tzinfo=data.LOCAL_TZ).timestamp()
    except (OverflowError, TypeError, ValueError):
        return None


def _target_day(row: pd.Series) -> date | None:
    value = row.get("target_date")
    try:
        if value is not None and not pd.isna(value):
            return date.fromisoformat(str(value)[:10])
        target_time = row.get("target_time")
        if target_time is None:
            return None
        stamp = pd.Timestamp(target_time)
        if pd.isna(stamp):
            return None
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        return stamp.tz_convert(data.LOCAL_TZ).date()
    except (OverflowError, TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=UTC) if result.tzinfo is None else result
    except ValueError:
        return None


class MetricsCollector:
    """Collect and cache a complete metrics payload for one app instance."""

    def __init__(self, base: BaseConfig, cache_seconds: float = CACHE_SECONDS) -> None:
        self.base = base
        self.cache_seconds = cache_seconds
        self._lock = Lock()
        self._cached_text: str | None = None
        self._cached_at: float | None = None
        self._forecast_cache: OrderedDict[Path, tuple[int, int, pd.DataFrame]] = OrderedDict()
        self._raw_cache: dict[Path, tuple[int, float | None, float | None, bool | None, int]] = {}

    def render(self) -> str:
        with self._lock:
            now = time.monotonic()
            if (
                self._cached_text is not None
                and self._cached_at is not None
                and now - self._cached_at < self.cache_seconds
            ):
                return self._cached_text

            started = time.perf_counter()
            registry = _Registry()
            errors = dict.fromkeys(_SECTIONS, 0)
            registry.add(
                "pricefc_build_info",
                1.0,
                env=os.environ.get("PRICEFC_ENV", "dev").lower(),
                git_sha=os.environ.get("PRICEFC_GIT_SHA", "unknown").lower(),
            )

            collectors: tuple[tuple[str, Callable[[_Registry], int]], ...] = (
                ("forecasts", self._collect_forecasts),
                ("state", self._collect_states),
                ("quality", self._collect_quality),
                ("raw", self._collect_raw),
                ("prefect", self._collect_prefect),
                ("mlflow", self._collect_mlflow),
            )
            for section, collect in collectors:
                try:
                    errors[section] += collect(registry)
                except Exception as exc:  # metrics must still render if a section fails
                    errors[section] += 1
                    log.warning("metrics_section_failed", section=section, error=str(exc))

            registry.add("pricefc_metrics_render_seconds", time.perf_counter() - started)
            for section, count in errors.items():
                registry.add("pricefc_metrics_errors", float(count), section=section)

            rendered = registry.render()
            self._cached_text = rendered
            self._cached_at = time.monotonic()
            return rendered

    def _read_forecast(self, path: Path) -> pd.DataFrame:
        stat = path.stat()
        cached = self._forecast_cache.get(path)
        if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            self._forecast_cache.move_to_end(path)
            return cached[2]

        frame = pd.read_parquet(path)
        self._forecast_cache.pop(path, None)
        self._forecast_cache[path] = (stat.st_mtime_ns, stat.st_size, frame)
        while len(self._forecast_cache) > FORECAST_CACHE_MAX:
            self._forecast_cache.popitem(last=False)
        return frame

    def _collect_forecasts(self, registry: _Registry) -> int:
        errors = 0
        for zone in data.ZONES:
            for role in ("champion", "challenger"):
                try:
                    files = data._forecast_files(self.base, zone, role)
                    if not files:
                        continue
                    newest = files[0]
                    frame = self._read_forecast(newest.path)
                    labels = {"zone": zone, "role": role}
                    origin = _origin_seconds(newest.origin)
                    if origin is not None:
                        registry.add(
                            "pricefc_forecast_last_origin_timestamp_seconds", origin, **labels
                        )
                    registry.add("pricefc_forecast_rows", float(len(frame)), **labels)
                    if frame.empty:
                        continue

                    first = frame.iloc[0]
                    target = _target_day(first)
                    if target is not None:
                        midnight = datetime.combine(target, local_time.min, tzinfo=data.LOCAL_TZ)
                        registry.add(
                            "pricefc_forecast_target_date_timestamp_seconds",
                            midnight.timestamp(),
                            **labels,
                        )
                    made_at = _timestamp_seconds(first.get("forecast_made_at"))
                    if made_at is not None:
                        registry.add(
                            "pricefc_forecast_made_at_timestamp_seconds", made_at, **labels
                        )
                    version = _number(first.get("model_version"))
                    if version is not None:
                        registry.add("pricefc_model_version", version, **labels)
                except Exception as exc:
                    errors += 1
                    log.warning("metrics_forecast_failed", zone=zone, role=role, error=str(exc))
        return errors

    def _collect_states(self, registry: _Registry) -> int:
        root = self.base.paths.data_root / "state"
        if not root.is_dir():
            return 0
        errors = 0
        for zone_dir in root.iterdir():
            if not zone_dir.is_dir() or zone_dir.name not in data.ZONES:
                continue
            for model_dir in zone_dir.iterdir():
                metadata_path = model_dir / "state.json"
                if not model_dir.is_dir() or not metadata_path.is_file():
                    continue
                try:
                    metadata = json.loads(metadata_path.read_text())
                    if not isinstance(metadata, dict):
                        raise ValueError("state metadata must be a JSON object")
                    origin = _origin_seconds(metadata.get("last_origin"))
                    if origin is not None:
                        registry.add(
                            "pricefc_state_last_origin_timestamp_seconds",
                            origin,
                            zone=zone_dir.name,
                            model=_label(str(metadata.get("spec", model_dir.name))),
                        )
                except Exception as exc:
                    errors += 1
                    log.warning("metrics_state_failed", path=str(metadata_path), error=str(exc))
        return errors

    def _collect_quality(self, registry: _Registry) -> int:
        errors = 0
        today = datetime.now(data.LOCAL_TZ).date()
        windows = ((7, "7d"), (14, "14d"))
        for zone in data.ZONES:
            role_files: dict[str, list[data._ForecastFile]] = {}
            for role in ("champion", "challenger"):
                try:
                    role_files[role] = data._forecast_files(self.base, zone, role)
                except Exception as exc:
                    errors += 1
                    role_files[role] = []
                    log.warning(
                        "metrics_forecast_list_failed", zone=zone, role=role, error=str(exc)
                    )

            recent_exists = any(
                today - timedelta(days=13) <= item.origin <= today
                for files in role_files.values()
                for item in files
            )
            try:
                actuals = data._actual_index(self.base, zone) if recent_exists else None
            except Exception as exc:
                actuals = None
                errors += 1
                log.warning("metrics_actuals_failed", zone=zone, error=str(exc))

            for role, files in role_files.items():
                cutoff = today - timedelta(days=13)
                selected = [item for item in files if cutoff <= item.origin <= today]
                parts: list[pd.DataFrame] = []
                for item in selected:
                    try:
                        frame = self._read_forecast(item.path)
                        if frame.empty:
                            continue
                        frame = frame.copy()
                        frame["origin_date"] = item.origin.isoformat()
                        parts.append(data._enrich(frame, actuals))
                    except Exception as exc:
                        errors += 1
                        log.warning(
                            "metrics_quality_file_failed",
                            zone=zone,
                            role=role,
                            origin=item.origin.isoformat(),
                            error=str(exc),
                        )
                all_rows = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
                for days, window in windows:
                    labels = {"zone": zone, "role": role, "window": window}
                    window_cutoff = (today - timedelta(days=days - 1)).isoformat()
                    if all_rows.empty:
                        window_rows = all_rows
                    else:
                        window_rows = all_rows[all_rows["origin_date"] >= window_cutoff]
                    try:
                        result = (
                            data._score(window_rows, self.base.quantiles)
                            if not window_rows.empty
                            else None
                        )
                        scored_days = 0
                        if not window_rows.empty:
                            scored_days = sum(
                                data._score(day_rows, self.base.quantiles) is not None
                                for _, day_rows in window_rows.groupby("origin_date", sort=True)
                            )
                        registry.add("pricefc_scored_days", float(scored_days), **labels)
                        if result is None:
                            continue
                        pinball = result.get("pinball")
                        naive = result.get("naive_7d_pinball")
                        coverage = result.get("coverage_90")
                        if pinball is not None:
                            registry.add("pricefc_pinball_mean", pinball, **labels)
                        if naive is not None:
                            registry.add("pricefc_naive_pinball_mean", naive, **labels)
                        if coverage is not None:
                            registry.add("pricefc_coverage_ratio", coverage, band="90", **labels)
                    except Exception as exc:
                        errors += 1
                        log.warning(
                            "metrics_quality_score_failed",
                            zone=zone,
                            role=role,
                            window=window,
                            error=str(exc),
                        )

        for zone, values in BACKTEST_BASELINES.items():
            registry.add("pricefc_backtest_pinball", values["pinball"], zone=zone)
        return errors

    def _raw_series(self, series_dir: Path) -> tuple[float | None, float | None, bool | None, int]:
        stat = series_dir.stat()
        cached = self._raw_cache.get(series_dir)
        if cached is not None and cached[0] == stat.st_mtime_ns:
            return cached[1], cached[2], cached[3], cached[4]

        snapshots: list[tuple[float, bool]] = []
        errors = 0
        for snapshot_dir in series_dir.glob("pulled_at=*"):
            if not snapshot_dir.is_dir():
                continue
            manifest_path = snapshot_dir / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text())
                pulled_at = _timestamp_seconds(manifest.get("pulled_at"))
                validation = manifest.get("validation", {})
                passed = isinstance(validation, dict) and validation.get("passed") is True
                if pulled_at is None:
                    raise ValueError("manifest has no valid pulled_at")
                snapshots.append((pulled_at, passed))
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                errors += 1
                fallback_name = snapshot_dir.name.removeprefix("pulled_at=")
                try:
                    fallback = (
                        datetime.strptime(fallback_name, "%Y-%m-%dT%H-%M-%SZ")
                        .replace(tzinfo=UTC)
                        .timestamp()
                    )
                except ValueError:
                    fallback = None
                if fallback is not None:
                    snapshots.append((fallback, False))
                log.warning("metrics_raw_manifest_failed", path=str(manifest_path), error=str(exc))

        latest = max((stamp for stamp, _ in snapshots), default=None)
        latest_valid = max((stamp for stamp, passed in snapshots if passed), default=None)
        latest_validity = None
        if latest is not None:
            latest_validity = next(passed for stamp, passed in snapshots if stamp == latest)
        self._raw_cache[series_dir] = (
            stat.st_mtime_ns,
            latest,
            latest_valid,
            latest_validity,
            errors,
        )
        return latest, latest_valid, latest_validity, errors

    def _collect_raw(self, registry: _Registry) -> int:
        raw_root = self.base.paths.raw
        if not raw_root.is_dir():
            return 0
        errors = 0
        for series_dir in raw_root.glob("*/*/*"):
            if not series_dir.is_dir():
                continue
            try:
                _latest, latest_valid, latest_validity, series_errors = self._raw_series(series_dir)
                errors += series_errors
                relative = series_dir.relative_to(raw_root).parts
                if len(relative) != 3:
                    continue
                labels = {"source": relative[0], "dataset": relative[1], "key": relative[2]}
                if latest_valid is not None:
                    registry.add(
                        "pricefc_raw_latest_valid_pulled_at_timestamp_seconds",
                        latest_valid,
                        **labels,
                    )
                if latest_validity is not None:
                    registry.add(
                        "pricefc_raw_latest_pull_valid",
                        1.0 if latest_validity else 0.0,
                        **labels,
                    )
            except Exception as exc:
                errors += 1
                log.warning("metrics_raw_series_failed", path=str(series_dir), error=str(exc))
        return errors

    def _prefect_post(self, base_url: str, path: str, body: dict[str, Any]) -> Any:
        response = requests.post(f"{base_url}/{path}", json=body, timeout=3)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError(f"Prefect {path} response must be a list")
        return payload

    def _collect_prefect(self, registry: _Registry) -> int:
        base_url = os.environ.get("PRICEFC_PREFECT_API_URL", "http://localhost:4200/api").rstrip(
            "/"
        )
        try:
            flows = self._prefect_post(
                base_url,
                "flows/filter",
                {"flows": {"name": {"any_": list(FLOWS)}}, "sort": "NAME_ASC", "limit": 100},
            )
            flow_ids = {
                str(item["name"]): str(item["id"])
                for item in flows
                if isinstance(item, dict) and item.get("name") in FLOWS and item.get("id")
            }
            now = datetime.now(UTC)
            scheduled_before = now + timedelta(hours=48)
            ids = sorted(flow_ids.values())
            if ids:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    history_future = executor.submit(
                        self._prefect_post,
                        base_url,
                        "flow_runs/filter",
                        {
                            "flows": {"id": {"any_": ids}},
                            "sort": "START_TIME_DESC",
                            "limit": 100,
                        },
                    )
                    scheduled_future = executor.submit(
                        self._prefect_post,
                        base_url,
                        "flow_runs/filter",
                        {
                            "flows": {"id": {"any_": ids}},
                            "flow_runs": {
                                "state": {"type": {"any_": ["SCHEDULED"]}},
                                "next_scheduled_start_time": {
                                    "after_": now.isoformat(),
                                    "before_": scheduled_before.isoformat(),
                                },
                            },
                            "sort": "NEXT_SCHEDULED_START_TIME_ASC",
                            "limit": 100,
                        },
                    )
                    history = history_future.result()
                    scheduled = scheduled_future.result()
            else:
                history, scheduled = [], []

            registry.add("pricefc_prefect_up", 1.0)
            for flow_name in FLOWS:
                flow_id = flow_ids.get(flow_name)
                runs = [
                    run
                    for run in history
                    if isinstance(run, dict)
                    and flow_id is not None
                    and str(run.get("flow_id")) == flow_id
                ]
                latest = max(
                    runs,
                    key=lambda run: _flow_run_time(run) or datetime.min.replace(tzinfo=UTC),
                    default=None,
                )
                if latest is not None:
                    run_timestamp = _flow_run_time(latest)
                    if run_timestamp is not None:
                        registry.add(
                            "pricefc_flow_last_run_timestamp_seconds",
                            run_timestamp.timestamp(),
                            flow=flow_name,
                        )
                    state = _flow_state(latest)
                    if state:
                        registry.add(
                            "pricefc_flow_last_run_state",
                            1.0,
                            flow=flow_name,
                            state=_label(state),
                        )

                successes = [run for run in runs if _flow_state(run).lower() == "completed"]
                last_success = max(
                    successes,
                    key=lambda run: _flow_success_time(run) or datetime.min.replace(tzinfo=UTC),
                    default=None,
                )
                if last_success is not None:
                    success_time = _flow_success_time(last_success)
                    if success_time is not None:
                        registry.add(
                            "pricefc_flow_last_success_timestamp_seconds",
                            success_time.timestamp(),
                            flow=flow_name,
                        )
                    duration = _flow_duration(last_success)
                    if duration is not None:
                        registry.add(
                            "pricefc_flow_last_success_duration_seconds",
                            duration,
                            flow=flow_name,
                        )

                count = sum(
                    isinstance(run, dict)
                    and flow_id is not None
                    and str(run.get("flow_id")) == flow_id
                    for run in scheduled
                )
                registry.add("pricefc_flow_runs_scheduled", float(count), flow=flow_name)
            return 0
        except Exception as exc:
            log.warning("metrics_prefect_failed", error=str(exc))
            registry.add("pricefc_prefect_up", 0.0)
            return 1

    def _collect_mlflow(self, registry: _Registry) -> int:
        url = os.environ.get("PRICEFC_MLFLOW_URL", "http://localhost:5000").rstrip("/")
        try:
            response = requests.get(f"{url}/health", timeout=3)
            response.raise_for_status()
            registry.add("pricefc_mlflow_up", 1.0)
            return 0
        except Exception as exc:
            log.warning("metrics_mlflow_failed", error=str(exc))
            registry.add("pricefc_mlflow_up", 0.0)
            return 1


def _flow_state(run: dict[str, Any]) -> str:
    state = run.get("state")
    if isinstance(state, dict) and state.get("type"):
        return str(state["type"])
    return str(run.get("state_type") or "")


def _flow_run_time(run: dict[str, Any]) -> datetime | None:
    for key in ("start_time", "state_timestamp", "created", "expected_start_time"):
        parsed = _iso_datetime(run.get(key))
        if parsed is not None:
            return parsed
    state = run.get("state")
    return _iso_datetime(state.get("timestamp")) if isinstance(state, dict) else None


def _flow_success_time(run: dict[str, Any]) -> datetime | None:
    for key in ("end_time", "state_timestamp"):
        parsed = _iso_datetime(run.get(key))
        if parsed is not None:
            return parsed
    state = run.get("state")
    return _iso_datetime(state.get("timestamp")) if isinstance(state, dict) else None


def _flow_duration(run: dict[str, Any]) -> float | None:
    start = _iso_datetime(run.get("start_time"))
    end = _iso_datetime(run.get("end_time"))
    if start is not None and end is not None:
        return max(0.0, (end - start).total_seconds())
    duration = run.get("total_run_time")
    if duration is None:
        return None
    if isinstance(duration, (int, float)):
        return max(0.0, float(duration))
    try:
        return max(0.0, pd.Timedelta(duration).total_seconds())
    except (TypeError, ValueError):
        return None
