"""Optuna hyperparameter search for the LightGBM quantile model (spec section 8.5).

Folds are time-ordered and expanding; every validation block ends before the backtest
evaluation window, which tuning never sees. The objective is mean pinball loss over
`tune_quantiles` (a subset of the configured quantiles, to keep trials affordable).
"""

from __future__ import annotations

import math
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import lightgbm as lgb
import numpy as np
import optuna
import pandas as pd
import structlog
from pydantic import BaseModel, ConfigDict, Field

from pricefc.backtest.metrics import pinball
from pricefc.datasets.build import TARGET
from pricefc.models.lgbm import feature_columns

log = structlog.get_logger(__name__)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StudyConfig(_Strict):
    n_trials: int = Field(ge=1)
    timeout_s: int = Field(ge=60)
    seed: int


class CVConfig(_Strict):
    n_folds: int = Field(ge=2)
    val_days: int = Field(ge=7)
    gap_days: int = Field(ge=0)


class SpaceParam(_Strict):
    low: float
    high: float
    log: bool = False
    int: bool = False


class HPOConfig(_Strict):
    study: StudyConfig
    cv: CVConfig
    tune_quantiles: list[float] = Field(min_length=1)
    max_estimators: int = Field(ge=10)
    early_stopping_rounds: int = Field(ge=5)
    fixed: dict[str, Any] = Field(default_factory=dict)
    space: dict[str, SpaceParam]


@dataclass(frozen=True)
class Fold:
    train_end: date  # last target day used for training (inclusive)
    val_start: date
    val_end: date  # inclusive


def make_folds(eval_start: date, n_folds: int, val_days: int, gap_days: int) -> list[Fold]:
    """Consecutive validation blocks ending the day before `eval_start`, oldest first.

    Each fold trains on target days <= val_start - 1 - gap_days (expanding window).
    """
    folds = []
    for k in range(n_folds, 0, -1):
        val_start = eval_start - timedelta(days=k * val_days)
        val_end = val_start + timedelta(days=val_days - 1)
        folds.append(Fold(val_start - timedelta(days=1 + gap_days), val_start, val_end))
    return folds


def suggest(trial: optuna.Trial, space: dict[str, SpaceParam]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for name, p in space.items():
        if p.int:
            params[name] = trial.suggest_int(name, int(p.low), int(p.high), log=p.log)
        else:
            params[name] = trial.suggest_float(name, p.low, p.high, log=p.log)
    return params


def _fit_eval(
    params: dict[str, Any],
    tau: float,
    x_tr: pd.DataFrame,
    y_tr: pd.Series,
    x_va: pd.DataFrame,
    y_va: pd.Series,
    cfg: HPOConfig,
    seed: int,
) -> tuple[float, int]:
    model = lgb.LGBMRegressor(
        objective="quantile",
        alpha=tau,
        n_estimators=cfg.max_estimators,
        random_state=seed,
        deterministic=True,
        force_row_wise=True,
        verbose=-1,
        **cfg.fixed,
        **params,
    )
    model.fit(
        x_tr,
        y_tr,
        eval_set=[(x_va, y_va)],
        eval_metric="quantile",
        callbacks=[lgb.early_stopping(cfg.early_stopping_rounds, verbose=False)],
    )
    best = int(model.best_iteration_ or cfg.max_estimators)
    pred = model.predict(x_va, num_iteration=best)
    return float(np.mean(pinball(y_va.to_numpy(), np.asarray(pred), tau))), best


def run_study(
    train_df: pd.DataFrame,
    eval_start: date,
    cfg: HPOConfig,
    *,
    study_name: str,
    storage: str,
    trial_callback: Any = None,
) -> optuna.Study:
    folds = make_folds(eval_start, cfg.cv.n_folds, cfg.cv.val_days, cfg.cv.gap_days)
    if folds[-1].val_end >= eval_start:
        raise AssertionError("tuning would touch the evaluation window")
    dates = pd.to_datetime(train_df["target_date"]).dt.date
    feats = feature_columns(train_df)
    data = []
    for f in folds:
        tr = train_df[dates <= f.train_end]
        va = train_df[(dates >= f.val_start) & (dates <= f.val_end)]
        data.append((tr[feats], tr[TARGET], va[feats], va[TARGET]))

    def objective(trial: optuna.Trial) -> float:
        params = suggest(trial, cfg.space)
        t0 = time.perf_counter()
        fold_losses, iters = [], []
        for i, (x_tr, y_tr, x_va, y_va) in enumerate(data):
            losses = []
            for tau in cfg.tune_quantiles:
                loss, best = _fit_eval(params, tau, x_tr, y_tr, x_va, y_va, cfg, cfg.study.seed)
                losses.append(loss)
                iters.append(best)
            fold_losses.append(float(np.mean(losses)))
            trial.report(float(np.mean(fold_losses)), step=i)
            if trial.should_prune():
                trial.set_user_attr("fold_losses", fold_losses)
                raise optuna.TrialPruned()
        trial.set_user_attr("fold_losses", fold_losses)
        trial.set_user_attr("best_iterations", iters)
        trial.set_user_attr("seconds", time.perf_counter() - t0)
        return float(np.mean(fold_losses))

    sampler = optuna.samplers.TPESampler(seed=cfg.study.seed)
    pruner = optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=1)
    study = optuna.create_study(
        study_name=study_name,
        storage=storage,
        direction="minimize",
        sampler=sampler,
        pruner=pruner,
        load_if_exists=True,
    )
    study.set_user_attr("folds", [{k: v.isoformat() for k, v in f.__dict__.items()} for f in folds])
    remaining = cfg.study.n_trials - len(study.trials)
    if remaining > 0:
        study.optimize(
            objective,
            n_trials=remaining,
            timeout=cfg.study.timeout_s,
            callbacks=[trial_callback] if trial_callback else None,
        )
    return study


def final_n_estimators(study: optuna.Study) -> int:
    """Median early-stopping iteration of the best trial (rounded up to a multiple of 10)."""
    iters: Sequence[int] = study.best_trial.user_attrs["best_iterations"]
    return int(math.ceil(float(np.median(iters)) / 10) * 10)


def load_hpo_config(path: Any) -> HPOConfig:
    from pricefc.config import load_yaml

    return HPOConfig.model_validate(load_yaml(path))


def tune_zone(
    base: Any,
    cfg: HPOConfig,
    zone: str,
    eval_start: date,
    train_ds: Any,
    *,
    storage: str = "sqlite:///optuna.db",
    out_dir: Any = None,
) -> tuple[optuna.Study, str, Any]:
    """Run (or resume) the study, log it to MLflow, write the tuned model config."""
    from pathlib import Path

    import matplotlib

    matplotlib.use("Agg")
    import mlflow
    import yaml
    from mlflow.data.pandas_dataset import from_pandas
    from optuna.visualization.matplotlib import (
        plot_optimization_history,
        plot_param_importances,
    )

    from pricefc.tracking.mlflow_utils import base_tags, setup_tracking, start_run

    train_df = train_ds.read()
    name = f"{zone}-lightgbm-{train_ds.version}-before-{eval_start}"
    setup_tracking(base)
    tags = base_tags(
        base,
        zone=zone,
        pipeline_stage="hpo",
        model_family="lightgbm",
        weather_source=train_ds.manifest["weather_source"],
        dataset_version=train_ds.version,
    )
    tags["optuna_study"] = name

    def log_trial(study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        with mlflow.start_run(run_name=f"trial-{trial.number}", nested=True, tags=tags):
            mlflow.log_params(trial.params)
            mlflow.log_param("state", trial.state.name)
            for i, loss in enumerate(trial.user_attrs.get("fold_losses", [])):
                mlflow.log_metric("fold_pinball", loss, step=i)
            if trial.value is not None:
                mlflow.log_metric("pinball_mean", trial.value)
            if "seconds" in trial.user_attrs:
                mlflow.log_metric("seconds", trial.user_attrs["seconds"])

    with start_run("training", tags, base, run_name=f"hpo-{name}") as run:
        mlflow.log_input(
            from_pandas(
                train_df,
                source=train_ds.data_file.resolve().as_uri(),
                name=train_ds.manifest["name"],
                targets="y",
            ),
            context="tuning",
        )
        mlflow.log_params(
            {
                "study": name,
                "sampler": "TPE",
                "pruner": "Median",
                "seed": cfg.study.seed,
                "n_trials": cfg.study.n_trials,
                "eval_start_untouched": str(eval_start),
                "tune_quantiles": ",".join(map(str, cfg.tune_quantiles)),
            }
        )
        mlflow.log_dict(cfg.model_dump(), "hpo_config.json")
        study = run_study(
            train_df, eval_start, cfg, study_name=name, storage=storage, trial_callback=log_trial
        )
        n_est = final_n_estimators(study)
        mlflow.log_params({f"best.{k}": v for k, v in study.best_params.items()})
        mlflow.log_metrics(
            {
                "best_pinball_mean": study.best_value,
                "best_n_estimators": n_est,
                "trials_complete": sum(t.state.name == "COMPLETE" for t in study.trials),
            }
        )
        mlflow.log_dict(study.user_attrs["folds"], "folds.json")
        mlflow.log_dict(optuna.importance.get_param_importances(study), "importances.json")
        with tempfile.TemporaryDirectory() as tmp:
            trials_csv = Path(tmp) / "trials.csv"
            study.trials_dataframe().to_csv(trials_csv, index=False)
            mlflow.log_artifact(str(trials_csv), "study")
        mlflow.log_figure(plot_optimization_history(study).figure, "optimization_history.png")
        mlflow.log_figure(plot_param_importances(study).figure, "param_importances.png")
        run_id = str(run.info.run_id)

    out_dir = Path(out_dir or "configs/models/tuned")
    out_dir.mkdir(parents=True, exist_ok=True)
    model_name = f"lightgbm_tuned_{zone}"
    body = {
        model_name: {
            "n_estimators": n_est,
            "seed": cfg.study.seed,
            "refit": "monthly",
            "params": {**cfg.fixed, **study.best_params},
        }
    }
    header = (
        f"# Generated by `pricefc tune lightgbm` - Optuna study {name}\n"
        f"# MLflow run {run_id}; tuned on {train_ds.version}, folds before {eval_start};\n"
        f"# best mean pinball (q{cfg.tune_quantiles}) {study.best_value:.4f}\n"
    )
    path = out_dir / f"lightgbm_{zone}.yaml"
    path.write_text(header + yaml.safe_dump(body, sort_keys=False))
    return study, run_id, path
