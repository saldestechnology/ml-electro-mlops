"""Run backtests for one zone, compare models, log everything to MLflow."""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import structlog

from pricefc.backtest.harness import BacktestResult, run_backtest, select_origins, summarize
from pricefc.backtest.metrics import compute_metrics, daily_losses, slice_metrics
from pricefc.backtest.stats import block_bootstrap_mean, diebold_mariano
from pricefc.config import BacktestConfig, BaseConfig
from pricefc.datasets.registry import StoredDataset, find_datasets
from pricefc.models.baselines import build_model

log = structlog.get_logger(__name__)


@dataclass
class ModelOutcome:
    result: BacktestResult
    metrics: dict[str, float]
    slices: pd.DataFrame
    daily: pd.DataFrame
    run_id: str | None = None
    train_start: date | None = None


def metric_key(spec: str) -> str:
    """MLflow-safe form of a model spec ('@' and '=' are not allowed in metric names)."""
    return spec.replace("@", "__").replace("=", "-")


def resolve_spec(
    spec: str, model_params: dict[str, dict[str, Any]], default_train_start: date | None
) -> tuple[str, dict[str, Any], date | None]:
    """'name@seed=3@train_start=2023-07-01' -> (name, params with overrides, train_start).

    Variants of one model can then be backtested side by side under identical conditions.
    """
    name, *overrides = spec.split("@")
    if name not in model_params:
        raise KeyError(f"unknown model {name!r}")
    params = dict(model_params[name])
    train_start = default_train_start
    for ov in overrides:
        key, _, value = ov.partition("=")
        if key == "seed":
            params["seed"] = int(value)
        elif key == "train_start":
            train_start = date.fromisoformat(value)
        elif key == "calibration":
            params["calibration_window_days"] = int(value)
        else:
            raise ValueError(f"unsupported override {key!r} in {spec!r}")
    return name, params, train_start


def latest_dataset(
    base: BaseConfig, zone: str, weather_kind: str, version: str | None = None
) -> StoredDataset:
    found = [d for d in find_datasets(base, zone) if d.manifest["weather_kind"] == weather_kind]
    if version:
        found = [d for d in found if d.version == version]
    if not found:
        raise FileNotFoundError(f"no {weather_kind} dataset for {zone} (version={version})")
    return max(found, key=lambda d: d.manifest["built_at"])


def compare(outcomes: dict[str, ModelOutcome], reference: str, cfg: BacktestConfig) -> pd.DataFrame:
    """Bootstrap CIs per model and versus the reference, plus Diebold-Mariano p-values.

    All models are aligned on the target days every model forecast.
    """
    common = sorted(set.intersection(*(set(o.daily.index) for o in outcomes.values())))
    b = cfg.bootstrap
    ref = outcomes[reference].daily.loc[common] if reference in outcomes else None
    rows = []
    for name, o in outcomes.items():
        d = o.daily.loc[common]
        row: dict[str, Any] = {"model": name, "days": len(common)}
        for metric in ("pinball", "ae"):
            ci = block_bootstrap_mean(
                d[metric].to_numpy(),
                block_length=b.block_length,
                n_resamples=b.n_resamples,
                seed=b.seed,
                level=b.level,
            )
            row[f"{metric}_mean"], row[f"{metric}_lo"], row[f"{metric}_hi"] = (
                ci.estimate,
                ci.lo,
                ci.hi,
            )
            if ref is not None and name != reference:
                diff = d[metric].to_numpy() - ref[metric].to_numpy()
                dci = block_bootstrap_mean(
                    diff,
                    block_length=b.block_length,
                    n_resamples=b.n_resamples,
                    seed=b.seed,
                    level=b.level,
                )
                dm = diebold_mariano(
                    d[metric].to_numpy(), ref[metric].to_numpy(), horizon=cfg.dm.horizon
                )
                row[f"{metric}_diff_vs_ref"] = dci.estimate
                row[f"{metric}_diff_lo"], row[f"{metric}_diff_hi"] = dci.lo, dci.hi
                row[f"{metric}_dm_stat"], row[f"{metric}_dm_p"] = dm.statistic, dm.p_value
                row[f"{metric}_skill_vs_ref"] = 1 - d[metric].mean() / ref[metric].mean()
        rows.append(row)
    return pd.DataFrame(rows).sort_values("pinball_mean").reset_index(drop=True)


def run_zone(
    base: BaseConfig,
    cfg: BacktestConfig,
    model_params: dict[str, dict[str, Any]],
    zone: str,
    *,
    models: list[str] | None = None,
    dev: bool = False,
    train_version: str | None = None,
    eval_version: str | None = None,
    log_to_mlflow: bool = True,
) -> tuple[dict[str, ModelOutcome], pd.DataFrame]:
    train_ds = latest_dataset(base, zone, "stitched", train_version)
    eval_ds = latest_dataset(base, zone, "true_lead", eval_version)
    train_df, eval_df = train_ds.read(), eval_ds.read()
    origins = select_origins(
        eval_df,
        start=cfg.eval_start,
        end=cfg.eval_end,
        every_n_days=cfg.dev_every_n_days if dev else 1,
    )
    log.info(
        "backtest",
        zone=zone,
        origins=len(origins),
        first=str(origins[0]),
        last=str(origins[-1]),
        train=train_ds.version,
        eval=eval_ds.version,
        dev=dev,
    )
    outcomes: dict[str, ModelOutcome] = {}
    for spec in models or cfg.models:
        name, params, train_start = resolve_spec(spec, model_params, cfg.train_start)
        model = build_model(name, base.quantiles, params)
        model.name = spec
        res = run_backtest(
            model, train_df, eval_df, origins, base.quantiles, train_start=train_start
        )
        outcome = ModelOutcome(
            res,
            compute_metrics(res.forecasts, base.quantiles),
            slice_metrics(res.forecasts, base.quantiles, base.timezone),
            daily_losses(res.forecasts, base.quantiles),
        )
        log.info(
            "model_done",
            model=spec,
            pinball=round(outcome.metrics["pinball_mean"], 3),
            mae=round(outcome.metrics["mae"], 3),
            **summarize(res),
        )
        outcome.train_start = train_start
        if log_to_mlflow:
            outcome.run_id = _log_model_run(
                base,
                cfg,
                zone,
                model,
                params,  # resolved, incl. variant overrides
                outcome,
                train_ds,
                eval_ds,
                train_df,
                eval_df,
                dev,
            )
        if spec in outcomes:
            raise ValueError(f"model spec {spec!r} listed twice")
        outcomes[spec] = outcome  # keyed by the full spec: variants must not collide
    table = compare(outcomes, cfg.reference_model, cfg)
    if log_to_mlflow:
        _log_compare_run(base, cfg, zone, table, outcomes, eval_ds, dev)
    return outcomes, table


# --- MLflow ----------------------------------------------------------------------------------


def _tags(
    base: BaseConfig, zone: str, eval_ds: StoredDataset, family: str, stage: str, dev: bool
) -> dict[str, str]:
    from pricefc.tracking.mlflow_utils import base_tags

    tags = base_tags(
        base,
        zone=zone,
        pipeline_stage=stage,
        model_family=family,
        weather_source=eval_ds.manifest["weather_source"],
        dataset_version=eval_ds.version,
    )
    tags["dev_mode"] = str(dev).lower()
    return tags


def _log_model_run(
    base: BaseConfig,
    cfg: BacktestConfig,
    zone: str,
    model: Any,
    params: dict[str, Any],
    o: ModelOutcome,
    train_ds: StoredDataset,
    eval_ds: StoredDataset,
    train_df: pd.DataFrame,
    eval_df: pd.DataFrame,
    dev: bool,
) -> str:
    import mlflow
    from mlflow.data.pandas_dataset import from_pandas

    from pricefc.tracking.mlflow_utils import setup_tracking, start_run

    setup_tracking(base)
    tags = _tags(base, zone, eval_ds, model.family, "backtest", dev)
    tags["train_dataset_version"] = train_ds.version
    tags.update(getattr(model, "run_tags", dict)())  # e.g. model licence, deployable
    with start_run("backtest", tags, base, run_name=f"{zone}-{model.name}") as run:
        mlflow.log_params(
            {
                **{f"model.{k}": v for k, v in params.items()},
                **{f"model.version.{k}": v for k, v in model.version_info().items()},
                "model": model.name,
                "train_start": str(o.train_start),
                "eval_first": o.result.forecasts["origin_date"].min(),
                "eval_last": o.result.forecasts["origin_date"].max(),
                "origins": o.result.forecasts["origin_date"].nunique(),
            }
        )
        mlflow.log_text(cfg.model_dump_json(indent=2), "config/backtest.json")
        for ds, df, ctx in ((train_ds, train_df, "training"), (eval_ds, eval_df, "evaluation")):
            mlflow.log_input(
                from_pandas(
                    df,
                    source=ds.data_file.resolve().as_uri(),
                    name=ds.manifest["name"],
                    targets="y",
                ),
                context=ctx,
            )
        mlflow.log_metrics({k: v for k, v in o.metrics.items()})
        mlflow.log_metrics({k: float(v) for k, v in summarize(o.result).items()})
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            o.result.forecasts.to_parquet(t / "forecasts.parquet", index=False)
            o.slices.to_csv(t / "slice_metrics.csv", index=False)
            o.daily.to_csv(t / "daily_losses.csv")
            o.result.fit_log().to_csv(t / "fit_log.csv", index=False)
            if getattr(model, "calibration_log", None):
                pd.DataFrame(model.calibration_log).to_csv(t / "calibration_log.csv", index=False)
            with contextlib.suppress(AttributeError):  # not every model has importances
                model.feature_importance().to_csv(t / "feature_importance_gain.csv")
            mlflow.log_artifacts(tmp)
        return str(run.info.run_id)


def _log_compare_run(
    base: BaseConfig,
    cfg: BacktestConfig,
    zone: str,
    table: pd.DataFrame,
    outcomes: dict[str, ModelOutcome],
    eval_ds: StoredDataset,
    dev: bool,
) -> str:
    import mlflow

    from pricefc.tracking.mlflow_utils import setup_tracking, start_run

    setup_tracking(base)
    tags = _tags(base, zone, eval_ds, "comparison", "compare", dev)
    with start_run("backtest", tags, base, run_name=f"{zone}-comparison") as run:
        mlflow.log_params(
            {
                "reference_model": cfg.reference_model,
                "bootstrap.n_resamples": cfg.bootstrap.n_resamples,
                "bootstrap.block_length_days": cfg.bootstrap.block_length,
                "bootstrap.level": cfg.bootstrap.level,
                "bootstrap.seed": cfg.bootstrap.seed,
                "bootstrap.method": "circular block, percentile CI",
                "dm.horizon": cfg.dm.horizon,
                "dm.correction": "Harvey-Leybourne-Newbold",
                "models": ",".join(outcomes),
            }
        )
        for _, row in table.iterrows():
            for k, v in row.items():
                if k != "model" and pd.notna(v):
                    mlflow.log_metric(f"{metric_key(row['model'])}.{k}", float(v))
        mlflow.log_dict({n: o.run_id for n, o in outcomes.items()}, "model_runs.json")
        with tempfile.TemporaryDirectory() as tmp:
            table.to_csv(Path(tmp) / "comparison.csv", index=False)
            mlflow.log_artifacts(tmp)
        return str(run.info.run_id)


def compare_runs(
    base: BaseConfig, cfg: BacktestConfig, run_ids: list[str], *, log_to_mlflow: bool = True
) -> pd.DataFrame:
    """Re-run the comparison from logged backtest runs (their daily losses), e.g. to combine
    runs from separate invocations. All runs must share the zone and evaluation dataset."""
    import mlflow

    from pricefc.tracking.mlflow_utils import setup_tracking

    os.environ.setdefault("MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR", "false")
    setup_tracking(base)
    outcomes: dict[str, ModelOutcome] = {}
    zones, versions = set(), set()
    for rid in run_ids:
        run = mlflow.get_run(rid)
        zones.add(run.data.tags["zone"])
        versions.add(run.data.tags["dataset_version"])
        path = mlflow.artifacts.download_artifacts(run_id=rid, artifact_path="daily_losses.csv")
        daily = pd.read_csv(path, index_col="target_date")
        name = run.data.params["model"]
        if name in outcomes:
            raise ValueError(f"two runs for model spec {name!r}")
        outcomes[name] = ModelOutcome(None, {}, pd.DataFrame(), daily, rid)  # type: ignore[arg-type]
    if len(zones) != 1 or len(versions) != 1:
        raise ValueError(f"runs mix zones {zones} or evaluation datasets {versions}")
    table = compare(outcomes, cfg.reference_model, cfg)
    if log_to_mlflow:
        zone, version = zones.pop(), versions.pop()
        eval_ds = next(d for d in find_datasets(base, zone) if d.version == version)
        _log_compare_run(base, cfg, zone, table, outcomes, eval_ds, dev=False)
    return table
