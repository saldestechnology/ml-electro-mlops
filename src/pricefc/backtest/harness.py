"""Rolling-origin, expanding-window backtest (spec section 9).

For each origin day D (in order): if a refit is due, the model is fitted on training-dataset
rows whose target day is <= D (those prices were published on D-1 at 13:00, before the
origin). It then predicts the evaluation-dataset rows of origin D. Nothing is shuffled.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from pricefc.backtest.metrics import quantile_columns, sort_quantiles
from pricefc.models.base import Forecaster

TARGET_COLUMNS = ["y", "y_n_periods", "y_is_pt15m"]


@dataclass
class FitRecord:
    origin_date: str
    train_rows: int
    train_first_target_date: str
    train_last_target_date: str
    seconds: float


@dataclass
class BacktestResult:
    model: str
    forecasts: pd.DataFrame
    fits: list[FitRecord] = field(default_factory=list)
    crossing_rows_fixed: int = 0
    fit_seconds: float = 0.0
    predict_seconds: float = 0.0

    def fit_log(self) -> pd.DataFrame:
        return pd.DataFrame([f.__dict__ for f in self.fits])


def select_origins(
    eval_df: pd.DataFrame,
    *,
    start: date | None = None,
    end: date | None = None,
    every_n_days: int = 1,
) -> list[date]:
    """Origins present in the evaluation dataset; every n-th day in dev mode."""
    days = sorted(date.fromisoformat(d) for d in eval_df["origin_date"].unique())
    days = [d for d in days if (start is None or d >= start) and (end is None or d <= end)]
    return days[::every_n_days]


def needs_refit(model: Forecaster, last_fit: date | None, origin: date) -> bool:
    if last_fit is None or model.refit == "every_origin":
        return True
    return (origin.year, origin.month) != (last_fit.year, last_fit.month)


@dataclass
class StepResult:
    """One origin: the forecast frame (before quantile sorting), and the fit if one ran."""

    frame: pd.DataFrame
    fit: FitRecord | None
    predict_seconds: float


def step(
    model: Forecaster,
    train_df: pd.DataFrame,
    rows: pd.DataFrame,
    day: date,
    last_fit: date | None,
    cols: Sequence[str],
    *,
    train_dates: np.ndarray | None = None,
    train_start: date | None = None,
) -> StepResult:
    """Advance `model` by one origin: refit if due on rows published by `day`, then predict
    `rows` (the evaluation rows of origin `day`). Shared by the backtest and live serving, so
    a served model goes through exactly the same sequence of calls as in its backtest."""
    fit = None
    if needs_refit(model, last_fit, day):
        if train_dates is None:
            train_dates = pd.to_datetime(train_df["target_date"]).dt.date.to_numpy()
        mask = train_dates <= day
        if train_start is not None:
            mask &= train_dates >= train_start
        train = train_df[mask]
        if train.empty:
            raise ValueError(f"no training rows for origin {day}")
        t0 = time.perf_counter()
        model.fit(train)
        fit = FitRecord(
            day.isoformat(),
            len(train),
            str(train["target_date"].min()),
            str(train["target_date"].max()),
            time.perf_counter() - t0,
        )
    origin_ts = pd.Timestamp(rows["origin"].iloc[0])
    t0 = time.perf_counter()
    # Models never see the target (or its metadata) at prediction time.
    pred = model.predict(origin_ts, rows.drop(columns=TARGET_COLUMNS, errors="ignore"))
    secs = time.perf_counter() - t0
    if list(pred.columns) != list(cols) or not pred.index.equals(rows.index):
        raise ValueError(f"{model.name}: prediction shape/columns do not match the contract")
    out = ["origin_date", "origin", "target_time", "target_date"]
    if "y" in rows:
        out.append("y")
    return StepResult(rows[out].join(pred), fit, secs)


def finish_forecasts(
    model_name: str, frames: list[pd.DataFrame], quantiles: Sequence[float]
) -> tuple[pd.DataFrame, int]:
    """Concatenate per-origin frames, reject NaN quantiles, repair crossing by sorting."""
    cols = quantile_columns(quantiles)
    forecasts = pd.concat(frames, ignore_index=True)
    n_nan = int(forecasts[cols].isna().any(axis=1).sum())
    if n_nan:
        raise ValueError(f"{model_name}: {n_nan} forecast rows contain NaN quantiles")
    forecasts, fixed = sort_quantiles(forecasts, quantiles)
    forecasts.insert(0, "model", model_name)
    return forecasts, fixed


def run_backtest(
    model: Forecaster,
    train_df: pd.DataFrame,
    eval_df: pd.DataFrame,
    origins: Sequence[date],
    quantiles: Sequence[float],
    *,
    train_start: date | None = None,
) -> BacktestResult:
    if not origins:
        raise ValueError("no origins to backtest")
    cols = quantile_columns(quantiles)
    train_dates = pd.to_datetime(train_df["target_date"]).dt.date.to_numpy()
    eval_by_origin = {k: v for k, v in eval_df.groupby("origin_date")}
    result = BacktestResult(model.name, pd.DataFrame())
    frames = []
    last_fit: date | None = None
    for day in origins:
        res = step(
            model,
            train_df,
            eval_by_origin[day.isoformat()],
            day,
            last_fit,
            cols,
            train_dates=train_dates,
            train_start=train_start,
        )
        if res.fit is not None:
            result.fits.append(res.fit)
            result.fit_seconds += res.fit.seconds
            last_fit = day
        result.predict_seconds += res.predict_seconds
        frames.append(res.frame)
    result.forecasts, result.crossing_rows_fixed = finish_forecasts(model.name, frames, quantiles)
    return result


def summarize(result: BacktestResult) -> dict[str, Any]:
    return {
        "origins": int(result.forecasts["origin_date"].nunique()),
        "rows": len(result.forecasts),
        "fits": len(result.fits),
        "fit_seconds": round(result.fit_seconds, 3),
        "predict_seconds": round(result.predict_seconds, 3),
        "crossing_rows_fixed": result.crossing_rows_fixed,
        "mean_train_rows": float(np.mean([f.train_rows for f in result.fits])),
    }
