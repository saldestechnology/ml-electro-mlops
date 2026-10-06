"""The morning forecast for one zone: served champion state -> D+1 forecasts.

`forecast_origin` is the whole step without Prefect, so the flow, the CLI and the tests run the
same code. Order matters for recovery: inputs are checked before the state is touched, the
state is backed up before it advances, forecasts are written before the state is saved. A run
that fails anywhere therefore leaves the previous state on disk and can simply be re-run; a
re-run of a day already served returns the forecast written then.

Layout under `<data_root>`:
  state/<zone>/<model>/                     the served state (state.pkl + state.json)
  state/<zone>/<model>.backups/<origin>/    the state as it was after <origin>
  forecasts/<zone>/<origin_date>.parquet    one file per origin served
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import structlog

from pricefc.config import BaseConfig
from pricefc.serving.state import META_FILE, ModelState, StateError

log = structlog.get_logger(__name__)

DEFAULT_MODEL = "ensemble_hourly_exp"


@dataclass
class ForecastResult:
    zone: str
    origin_date: date
    model_version: str
    forecasts: pd.DataFrame  # the rows of `origin_date` only
    caught_up: list[date] = field(default_factory=list)  # missed origins served first
    path: Path | None = None
    run_id: str | None = None
    role: str = "champion"


def state_dir(base: BaseConfig, zone: str, model: str, state_root: Path | None = None) -> Path:
    return (state_root or base.paths.data_root / "state") / zone / model


def backups_dir(directory: Path) -> Path:
    return directory.with_name(f"{directory.name}.backups")


def forecast_path(base: BaseConfig, zone: str, day: date, role: str = "champion") -> Path:
    root = base.paths.data_root / "forecasts" / zone
    if role == "challenger":
        root /= "challenger"
    elif role != "champion":
        raise ValueError(f"unknown forecast role {role!r}")
    return root / f"{day.isoformat()}.parquet"


def backup_state(directory: Path, state: ModelState, keep: int) -> Path | None:
    """Copy the saved state to `<model>.backups/<last_origin>/`, keep the newest `keep`."""
    if state.last_origin is None or not (directory / META_FILE).exists():
        return None
    root = backups_dir(directory)
    dest = root / state.last_origin.isoformat()
    if dest.exists():
        shutil.rmtree(dest)
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(directory, dest)
    for old in sorted(p for p in root.iterdir() if p.is_dir())[: -keep or None]:
        shutil.rmtree(old)
    return dest


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def _live_frame(live: Any) -> tuple[pd.DataFrame, str | None]:
    """A LiveRows (frame + directory written) or a bare DataFrame."""
    if isinstance(live, pd.DataFrame):
        return live, None
    path = getattr(live, "path", None)
    return live.frame, (Path(path).name if path is not None else None)


def _check_no_gap(
    zone: str, state: ModelState, day: date, eval_df: pd.DataFrame, manifest: dict[str, Any]
) -> None:
    """Every origin between the state's last one and `day` must be in the evaluation dataset.

    `advance` serves only the origins the dataset holds, like the backtest. The backtest skips
    days the build dropped for lack of a target; a day that is merely not built yet (prices
    not ingested, dataset not rebuilt) would be skipped for good and the served state would
    no longer match its backtest. So: only gaps the build itself reports are allowed."""
    if state.last_origin is None:
        return
    have = set(eval_df["origin_date"].astype(str))
    dropped = {
        (date.fromisoformat(t) - timedelta(days=1)).isoformat()
        for t in manifest.get("build", {}).get("target_dates_dropped", [])
    }
    gap = [
        d.isoformat()
        for d in (
            state.last_origin + timedelta(days=i) for i in range(1, (day - state.last_origin).days)
        )
        if d.isoformat() not in have and d.isoformat() not in dropped
    ]
    if gap:
        raise StateError(
            f"{zone}: evaluation dataset lacks origins {gap} between the state's last origin "
            f"{state.last_origin} and {day}; ingest prices and rebuild datasets first"
        )


def ensure_state(
    zone: str, model: str, alias: str, directory: Path, keep_backups: int
) -> tuple[ModelState, str]:
    """Load the local state, pulling the registry version `alias` points at if the local copy
    is missing or comes from another version. A replaced local state is backed up first."""
    from pricefc.serving.registry import pull_state, resolve

    version = resolve(zone, alias)
    state = ModelState.load(directory) if (directory / META_FILE).exists() else None
    if state is None or state.source_version != version:
        if state is not None:
            backup_state(directory, state, keep_backups)
        log.info(
            "pull_state",
            zone=zone,
            alias=alias,
            version=version,
            local_version=state.source_version if state else None,
        )
        state = pull_state(zone, version, directory)
    if state.zone != zone or state.spec != model:
        raise StateError(f"{directory} holds {state.zone}/{state.spec}, not {zone}/{model}")
    return state, version


def forecast_origin(
    base: BaseConfig,
    zone: str,
    day: date,
    *,
    model: str = DEFAULT_MODEL,
    alias: str = "champion",
    role: str | None = None,
    state_root: Path | None = None,
    live: Any = None,
    keep_backups: int = 7,
    features: Any = None,
    ingest: Any = None,
) -> ForecastResult:
    """Forecast D+1 from origin `day` with the served state of `alias`, catching up any
    missed origins first. `live` is a `LiveRows` (or its frame); built when None."""
    from pricefc.backtest import run as bt_run
    from pricefc.tracking.mlflow_utils import setup_tracking

    role = role or ("champion" if alias == "champion" else "challenger")
    if role not in {"champion", "challenger"}:
        raise ValueError(f"unknown forecast role {role!r}")
    setup_tracking(base)
    directory = state_dir(base, zone, model, state_root)
    state, version = ensure_state(zone, model, alias, directory, keep_backups)

    if state.last_origin is not None and state.last_origin >= day:
        path = forecast_path(base, zone, day, role)
        if not path.exists():
            raise StateError(
                f"{zone}: origin {day} is not after the state's last origin "
                f"{state.last_origin} and was never written ({path})"
            )
        log.info("already_served", zone=zone, day=str(day), path=str(path))
        written = pd.read_parquet(path)
        made_with = str(written["model_version"].iloc[0]) if len(written) else version
        return ForecastResult(zone, day, made_with, written, [], path, role=role)

    train_ds = bt_run.latest_dataset(base, zone, "stitched")
    eval_ds = bt_run.latest_dataset(base, zone, "true_lead")
    train_df, eval_df = train_ds.read(), eval_ds.read()
    _check_no_gap(zone, state, day, eval_df, eval_ds.manifest)
    if live is None:
        from pricefc.config import load_features_config, load_ingest_config
        from pricefc.datasets.live import build_live_rows

        live = build_live_rows(
            base,
            features or load_features_config(Path("configs/features.yaml")),
            ingest or load_ingest_config(Path("configs/ingest.yaml")),
            zone,
            day,
        )
    live_df, live_name = _live_frame(live)
    if list(live_df.columns) != list(eval_df.columns):
        missing = sorted(set(eval_df.columns) - set(live_df.columns))
        extra = sorted(set(live_df.columns) - set(eval_df.columns))
        raise ValueError(
            f"{zone}: live columns differ from eval dataset {eval_ds.version} "
            f"(missing {missing}, extra {extra}, or a different order)"
        )
    if set(live_df["origin_date"].astype(str)) != {day.isoformat()}:
        raise ValueError(f"{zone}: live rows are not all for origin {day}")
    combined = pd.concat(
        [eval_df[eval_df["origin_date"].astype(str) < day.isoformat()], live_df],
        ignore_index=True,
    )

    backup_state(directory, state, keep_backups)
    before = len(state.history)
    forecasts = state.advance(train_df, combined, day)
    served = [date.fromisoformat(h["origin_date"]) for h in state.history[before:]]
    refit = bool(state.history[-1]["refit"])
    state.datasets = {
        "train": train_ds.version,
        "eval": eval_ds.version,
        "live": live_name or day.isoformat(),
    }

    made_at = pd.Timestamp.now(tz="UTC")
    forecasts = forecasts.assign(model_version=version, forecast_made_at=made_at)
    paths: dict[date, Path] = {}
    for origin, frame in forecasts.groupby("origin_date", sort=True):
        d = date.fromisoformat(str(origin))
        paths[d] = forecast_path(base, zone, d, role)
        _write_parquet(frame.reset_index(drop=True), paths[d])
    state.save(directory)

    today = forecasts[forecasts["origin_date"] == day.isoformat()].reset_index(drop=True)
    caught_up = [d for d in served if d != day]
    run_id = _log_run(base, state, version, day, today, served, refit, paths[day], role)
    log.info(
        "forecast_written",
        zone=zone,
        day=str(day),
        version=version,
        role=role,
        caught_up=[str(d) for d in caught_up],
        refit=refit,
        path=str(paths[day]),
    )
    return ForecastResult(zone, day, version, today, caught_up, paths[day], run_id, role)


def _log_run(
    base: BaseConfig,
    state: ModelState,
    version: str,
    day: date,
    forecasts: pd.DataFrame,
    served: list[date],
    refit: bool,
    path: Path,
    role: str,
) -> str:
    import mlflow

    from pricefc.serving.registry import model_name
    from pricefc.tracking.mlflow_utils import base_tags, start_run

    tags = base_tags(
        base,
        zone=state.zone,
        pipeline_stage="forecast",
        model_family=getattr(state.model, "family", "unknown"),
        dataset_version=state.datasets.get("eval", "none"),
    )
    tags.update(
        {
            "origin_date": day.isoformat(),
            "model_name": model_name(state.zone),
            "model_version": version,
            "model_spec": state.spec,
            "role": role,
            **{f"dataset.{k}": v for k, v in state.datasets.items()},
        }
    )
    with start_run("forecast-live", tags, base, run_name=f"{state.zone}-{day}-{role}") as run:
        mlflow.log_params(
            {
                "model": state.spec,
                "last_fit": str(state.last_fit),
                "caught_up": ",".join(str(d) for d in served if d != day) or "none",
            }
        )
        mlflow.log_metrics(
            {
                "n_rows": len(forecasts),
                "n_origins_served": len(served),
                "refit": int(refit),
            }
        )
        mlflow.log_artifact(str(path), artifact_path="forecasts")
        return str(run.info.run_id)
