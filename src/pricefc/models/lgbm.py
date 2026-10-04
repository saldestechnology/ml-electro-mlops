"""LightGBM quantile forecaster (spec section 8.2): one model per quantile, shared features."""

from __future__ import annotations

import importlib.metadata
import os
from collections.abc import Sequence
from typing import Any

import lightgbm as lgb
import pandas as pd

from pricefc.backtest.metrics import qcol
from pricefc.datasets.build import META_COLUMNS, TARGET
from pricefc.models.base import Refit


def feature_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in (*META_COLUMNS, TARGET)]


class LGBMQuantile:
    """Gradient-boosted quantile regression. Quantile crossing is repaired by the harness
    (row-wise sort)."""

    family = "lightgbm"

    def __init__(
        self,
        quantiles: Sequence[float],
        params: dict[str, Any] | None = None,
        n_estimators: int = 500,
        seed: int = 42,
        refit: Refit = "monthly",
        name: str = "lightgbm",
        exclude_features: Sequence[str] = (),
    ) -> None:
        self.quantiles = list(quantiles)
        self.lgb_params = dict(params or {})
        self.n_estimators = n_estimators
        self.seed = seed
        self.refit = refit
        self.name = name
        self.exclude_features = list(exclude_features)
        self.models: dict[float, lgb.LGBMRegressor] = {}
        self.features: list[str] = []
        self.importance_history: list[pd.Series] = []

    def _estimator(self, tau: float) -> lgb.LGBMRegressor:
        return lgb.LGBMRegressor(
            objective="quantile",
            alpha=tau,
            n_estimators=self.n_estimators,
            random_state=self.seed,
            deterministic=True,
            force_row_wise=True,
            n_jobs=os.cpu_count(),
            verbose=-1,
            **self.lgb_params,
        )

    def fit(self, train: pd.DataFrame) -> None:
        self.features = [c for c in feature_columns(train) if c not in self.exclude_features]
        x, y = train[self.features], train[TARGET]
        self.models = {tau: self._estimator(tau).fit(x, y) for tau in self.quantiles}
        gain = self.models[0.5].booster_.feature_importance(importance_type="gain")
        self.importance_history.append(pd.Series(gain, index=self.features))

    def predict(self, origin: pd.Timestamp, features: pd.DataFrame) -> pd.DataFrame:
        if not self.models:
            raise RuntimeError("fit() first")
        x = features[self.features]
        return pd.DataFrame(
            {qcol(tau): m.predict(x) for tau, m in self.models.items()}, index=features.index
        )

    def feature_importance(self) -> pd.DataFrame:
        """Gain importance of the median model, mean and share over all refits."""
        imp = pd.concat(self.importance_history, axis=1).fillna(0.0)
        mean = imp.mean(axis=1)
        return pd.DataFrame({"gain_mean": mean, "gain_share": mean / mean.sum()}).sort_values(
            "gain_mean", ascending=False
        )

    def params(self) -> dict[str, Any]:
        return {
            **self.lgb_params,
            "n_estimators": self.n_estimators,
            "seed": self.seed,
            "refit": self.refit,
            "exclude_features": ",".join(self.exclude_features),
        }

    def version_info(self) -> dict[str, Any]:
        return {"lightgbm": importlib.metadata.version("lightgbm")}
