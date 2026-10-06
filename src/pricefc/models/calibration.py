"""Rolling quantile recalibration of any forecaster (spec 8.2: optional conformal calibration).

The wrapper keeps the base model's raw forecasts. At each origin D it learns, for every
quantile tau, the offset Q_tau(y - q_tau) over its own forecasts for target days in the last
`window_days` that are <= D. Realised prices reach it only through `fit(train)`, i.e. the
harness's published-before-origin training rows, so it cannot see the future. After the
shift, each quantile is calibrated on the recent window, which corrects both bias and
interval width, e.g. when training weather is more accurate than live weather.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

from pricefc.backtest.metrics import qcol
from pricefc.models.base import Forecaster, Refit


class RecalibratedForecaster:
    refit: Refit = "every_origin"  # calibration updates daily; the base keeps its own cadence

    def __init__(
        self,
        base: Forecaster,
        quantiles: Sequence[float],
        window_days: int = 28,
        min_days: int = 7,
    ) -> None:
        self.base = base
        self.quantiles = list(quantiles)
        self.window_days = window_days
        self.min_days = min_days
        self.name = base.name
        self.family = base.family
        self._raw: list[pd.DataFrame] = []
        self._offsets = dict.fromkeys(self.quantiles, 0.0)
        self._base_month: tuple[int, int] | None = None
        self.calibration_log: list[dict[str, Any]] = []

    def fit(self, train: pd.DataFrame) -> None:
        last = pd.Timestamp(train["target_date"].max())
        month = (last.year, last.month)
        if self.base.refit == "every_origin" or month != self._base_month:
            self.base.fit(train)
            self._base_month = month
        self._update_offsets(train, last)

    def _update_offsets(self, train: pd.DataFrame, last: pd.Timestamp) -> None:
        if not self._raw:
            return
        start = (last - pd.Timedelta(days=self.window_days - 1)).date().isoformat()
        # `last` only moves forward, so forecasts wholly before the window are never read
        # again; dropping them bounds a served model's state.
        self._raw = [r for r in self._raw if r["target_date"].astype(str).max() >= start]
        if not self._raw:
            return
        raw = pd.concat(self._raw, ignore_index=True)
        raw = raw[(raw["target_date"] >= start) & (raw["target_date"] <= last.date().isoformat())]
        known = raw.merge(train[["target_time", "y"]], on="target_time", how="inner")
        days = known["target_date"].nunique()
        if days < self.min_days:
            return
        y = known["y"].to_numpy(dtype="float64")
        for tau in self.quantiles:
            resid = y - known[qcol(tau)].to_numpy(dtype="float64")
            self._offsets[tau] = float(np.quantile(resid, tau))
        self.calibration_log.append(
            {
                "last_target_date": last.date().isoformat(),
                "days": days,
                **{f"offset_{qcol(t)}": o for t, o in self._offsets.items()},
            }
        )

    def predict(self, origin: pd.Timestamp, features: pd.DataFrame) -> pd.DataFrame:
        raw = self.base.predict(origin, features)
        stored = raw.copy()
        stored["target_time"] = features["target_time"].to_numpy()
        stored["target_date"] = features["target_date"].to_numpy()
        self._raw.append(stored)
        out = raw.copy()
        for tau in self.quantiles:
            out[qcol(tau)] = raw[qcol(tau)] + self._offsets[tau]
        return out

    def params(self) -> dict[str, Any]:
        return {
            **self.base.params(),
            "calibration": "rolling_quantile_offset",
            "calibration_window_days": self.window_days,
            "calibration_min_days": self.min_days,
        }

    def version_info(self) -> dict[str, Any]:
        return self.base.version_info()

    def run_tags(self) -> dict[str, str]:
        tags: dict[str, str] = getattr(self.base, "run_tags", dict)()
        return tags

    def feature_importance(self) -> pd.DataFrame:
        fi = getattr(self.base, "feature_importance", None)
        if fi is None:
            raise AttributeError("base model has no feature importance")
        result: pd.DataFrame = fi()
        return result
