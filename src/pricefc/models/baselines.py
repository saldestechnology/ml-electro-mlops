"""Baselines (spec section 8.1): seasonal naive and a StatsForecast MSTL model."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Sequence
from typing import Any, Literal

import numpy as np
import pandas as pd

from pricefc.backtest.metrics import qcol
from pricefc.models.base import Refit


class SeasonalNaive:
    """Point = the price of the same local hour `lag_days` before the target, or with
    `lag_days="weekday"` the standard EPF naive (1 day for Tue-Fri, 7 days for Sat-Mon).

    Quantiles add empirical quantiles of this method's own errors over the last
    `residual_days` of training data, optionally per local hour. If the lag is missing
    (e.g. an excluded source day, or an hour that did not exist on a DST day) the point
    falls back to the 7-day same-hour mean, then to day D's mean.
    """

    family = "seasonal_naive"

    def __init__(
        self,
        lag_days: int | Literal["weekday"],
        quantiles: Sequence[float],
        residual_days: int = 56,
        by_hour: bool = True,
        refit: Refit = "monthly",
    ) -> None:
        self.lag_days = lag_days
        self.quantiles = list(quantiles)
        self.residual_days = residual_days
        self.by_hour = by_hour
        self.refit = refit
        self.name = "naive_weekday" if lag_days == "weekday" else f"seasonal_naive_{lag_days}d"
        self._offsets: pd.DataFrame | None = None
        self.fallbacks = 0

    def _point(self, rows: pd.DataFrame) -> tuple[np.ndarray, int]:
        if self.lag_days == "weekday":
            # Mon (0), Sat (5), Sun (6) targets follow a day of a different type: use D-6.
            last_week = rows["cal_dow"].isin([0, 5, 6]).to_numpy()
            point = np.where(last_week, rows["p_lag7d"], rows["p_lag1d"]).astype("float64")
        else:
            point = rows[f"p_lag{self.lag_days}d"].to_numpy(dtype="float64")
        missing = np.isnan(point)
        n_missing = int(missing.sum())
        for fallback in ("p_samehour_mean7d", "p_dayD_mean"):
            fill = rows[fallback].to_numpy(dtype="float64")
            point = np.where(np.isnan(point), fill, point)
        return point, n_missing

    def fit(self, train: pd.DataFrame) -> None:
        last = pd.Timestamp(train["target_date"].max())
        recent = train[
            pd.to_datetime(train["target_date"]) > last - pd.Timedelta(days=self.residual_days)
        ]
        point, _ = self._point(recent)
        resid = recent["y"].to_numpy(dtype="float64") - point
        frame = pd.DataFrame({"hour": recent["cal_hour"].to_numpy(), "resid": resid}).dropna()
        keys = frame["hour"] if self.by_hour else np.zeros(len(frame), dtype=int)
        grouped = frame.groupby(keys)["resid"]
        self._offsets = pd.DataFrame({qcol(q): grouped.quantile(q) for q in self.quantiles})

    def predict(self, origin: pd.Timestamp, features: pd.DataFrame) -> pd.DataFrame:
        if self._offsets is None:
            raise RuntimeError("fit() first")
        point, n_missing = self._point(features)
        self.fallbacks += n_missing
        keys = (
            features["cal_hour"].to_numpy() if self.by_hour else np.zeros(len(features), dtype=int)
        )
        offsets = self._offsets.reindex(keys).to_numpy(dtype="float64")
        out = point[:, None] + offsets
        return pd.DataFrame(out, columns=self._offsets.columns, index=features.index)

    def params(self) -> dict[str, Any]:
        return {
            "lag_days": self.lag_days,
            "residual_days": self.residual_days,
            "by_hour": self.by_hour,
            "refit": self.refit,
        }

    def version_info(self) -> dict[str, Any]:
        return {}


class MSTLForecaster:
    """StatsForecast MSTL (daily + weekly seasonality, AutoETS trend) on the price history.

    Uses only the target series: hourly prices up to the end of day D, which are all
    published before the origin. Refit at every origin (fast). Quantiles come from the
    model's Gaussian prediction intervals.
    """

    family = "statsforecast"
    refit: Refit = "every_origin"

    def __init__(self, quantiles: Sequence[float], context_days: int = 56) -> None:
        self.quantiles = list(quantiles)
        self.context_days = context_days
        self.name = "mstl_ets"
        self._history: pd.Series | None = None
        self.interpolated = 0
        levels = set()
        for q in self.quantiles:
            if q != 0.5:
                levels.add(round(abs(1 - 2 * q) * 100))
        self._levels = sorted(levels)

    def fit(self, train: pd.DataFrame) -> None:
        series = train.set_index("target_time")["y"].sort_index()
        series = series[~series.index.duplicated()]
        end = series.index.max()
        series = series[series.index > end - pd.Timedelta(days=self.context_days)]
        full = series.reindex(pd.date_range(series.index.min(), end, freq="h"))
        # MSTL needs a regular series; gaps (excluded source days) are interpolated here
        # only, as model input, and counted.
        self.interpolated += int(full.isna().sum())
        self._history = full.interpolate(limit_direction="both")

    def predict(self, origin: pd.Timestamp, features: pd.DataFrame) -> pd.DataFrame:
        from statsforecast import StatsForecast
        from statsforecast.models import MSTL, AutoETS

        if self._history is None:
            raise RuntimeError("fit() first")
        hist = self._history
        targets = pd.DatetimeIndex(features["target_time"])
        steps = int((targets.max() - hist.index.max()) / pd.Timedelta(hours=1))
        df = pd.DataFrame(
            {
                "unique_id": "y",
                "ds": pd.DatetimeIndex(hist.index).tz_localize(None),
                "y": hist.to_numpy(),
            }
        )
        model = MSTL(season_length=[24, 168], trend_forecaster=AutoETS(model="ZZN"))
        fc = StatsForecast(models=[model], freq="h").forecast(df=df, h=steps, level=self._levels)
        fc.index = pd.DatetimeIndex(fc["ds"]).tz_localize("UTC")
        fc = fc.reindex(targets)
        out = {}
        for q in self.quantiles:
            if q == 0.5:
                out[qcol(q)] = fc["MSTL"].to_numpy()
            else:
                level = round(abs(1 - 2 * q) * 100)
                side = "lo" if q < 0.5 else "hi"
                out[qcol(q)] = fc[f"MSTL-{side}-{level}"].to_numpy()
        return pd.DataFrame(out, index=features.index)

    def params(self) -> dict[str, Any]:
        return {
            "context_days": self.context_days,
            "season_length": "24,168",
            "trend": "AutoETS(ZZN)",
            "refit": self.refit,
        }

    def version_info(self) -> dict[str, Any]:
        return {"statsforecast": importlib.metadata.version("statsforecast")}


def build_model(name: str, quantiles: Sequence[float], params: dict[str, Any]) -> Any:
    """Build a model; `calibration_window_days` wraps it in rolling quantile recalibration."""
    params = dict(params)
    window = params.pop("calibration_window_days", None)
    model = _build_base(name, quantiles, params)
    if window:
        from pricefc.models.calibration import RecalibratedForecaster

        return RecalibratedForecaster(model, quantiles, window_days=int(window))
    return model


def _build_base(name: str, quantiles: Sequence[float], params: dict[str, Any]) -> Any:
    if name.startswith("seasonal_naive") or name == "naive_weekday":
        return SeasonalNaive(quantiles=quantiles, **params)
    if name == "mstl_ets":
        return MSTLForecaster(quantiles=quantiles, **params)
    if name.startswith("lightgbm"):
        from pricefc.models.lgbm import LGBMQuantile

        return LGBMQuantile(quantiles=quantiles, name=name, **params)
    if name.startswith("timesfm"):
        from pricefc.models.timesfm import TimesFMForecaster

        return TimesFMForecaster(quantiles, name=name, **params)
    raise KeyError(f"unknown model {name!r}")
