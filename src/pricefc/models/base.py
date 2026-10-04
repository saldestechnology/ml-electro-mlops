"""The interface every forecaster implements (spec section 7)."""

from __future__ import annotations

from typing import Any, Literal, Protocol

import pandas as pd

Refit = Literal["every_origin", "monthly"]


class Forecaster(Protocol):
    """A day-ahead quantile forecaster.

    `fit` receives dataset rows whose targets were published by the origin (target_date <=
    origin_date). `predict` receives the feature rows of one origin and returns a frame
    indexed like `features` (one row per target hour) with one column per quantile
    (`q05`, ..., `q95`). The point forecast is `q50`.
    """

    name: str
    family: str
    refit: Refit

    def fit(self, train: pd.DataFrame) -> None: ...

    def predict(self, origin: pd.Timestamp, features: pd.DataFrame) -> pd.DataFrame: ...

    def params(self) -> dict[str, Any]: ...

    def version_info(self) -> dict[str, Any]: ...
