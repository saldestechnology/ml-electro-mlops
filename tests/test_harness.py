from datetime import date
from functools import cache
from typing import Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from pricefc.backtest.harness import needs_refit, run_backtest, select_origins
from pricefc.backtest.metrics import qcol
from pricefc.models.baselines import SeasonalNaive
from tests.test_leakage import builder

QS = [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95]


@cache
def synthetic_dataset() -> pd.DataFrame:
    df, _ = builder(3).build(date(2025, 3, 10), date(2025, 4, 17))
    return df


class Spy:
    """Records what it was trained on; predicts a fixed fan."""

    name, family = "spy", "test"

    def __init__(self, refit: str) -> None:
        self.refit = refit
        self.seen: list[tuple[str, str]] = []  # (latest train target_date, origin predicted)
        self._last = ""

    def fit(self, train: pd.DataFrame) -> None:
        self._last = str(train["target_date"].max())

    def predict(self, origin: pd.Timestamp, features: pd.DataFrame) -> pd.DataFrame:
        self.seen.append((self._last, str(features["origin_date"].iloc[0])))
        return pd.DataFrame({qcol(q): float(i) for i, q in enumerate(QS)}, index=features.index)

    def params(self) -> dict[str, Any]:
        return {}

    def version_info(self) -> dict[str, Any]:
        return {}


@settings(max_examples=25)
# skip >= 1: train and eval are the same synthetic frame, so the first origin has no
# earlier targets to train on (real runs train on a longer history).
@given(st.integers(1, 6), st.sampled_from(["every_origin", "monthly"]), st.integers(1, 10))
def test_training_never_sees_targets_after_the_origin(every: int, refit: str, skip: int) -> None:
    df = synthetic_dataset()
    origins = select_origins(df, every_n_days=every)[skip:]
    assume(origins)
    spy = Spy(refit)
    res = run_backtest(spy, df, df, origins, QS)  # type: ignore[arg-type]
    for last_train, origin in spy.seen:
        assert last_train <= origin  # target day <= D: published before the origin
    fc = res.forecasts
    assert (fc["target_date"] > fc["origin_date"]).all()  # predictions are for D+1
    assert sorted(fc["origin_date"].unique()) == [d.isoformat() for d in origins]
    assert (np.diff(fc[[qcol(q) for q in QS]].to_numpy(), axis=1) >= 0).all()


def test_monthly_refit_schedule() -> None:
    spy = Spy("monthly")
    assert needs_refit(spy, None, date(2025, 3, 31))  # type: ignore[arg-type]
    assert not needs_refit(spy, date(2025, 3, 10), date(2025, 3, 31))  # type: ignore[arg-type]
    assert needs_refit(spy, date(2025, 3, 31), date(2025, 4, 1))  # type: ignore[arg-type]


def test_train_start_excludes_early_rows() -> None:
    df = synthetic_dataset()
    res = run_backtest(
        Spy("monthly"),
        df,
        df,
        select_origins(df)[5:7],
        QS,  # type: ignore[arg-type]
        train_start=date(2025, 3, 14),
    )
    assert res.fits[0].train_first_target_date == "2025-03-14"


def test_seasonal_naive_point_is_last_weeks_price() -> None:
    df = synthetic_dataset()
    origins = select_origins(df)[-5:]
    res = run_backtest(SeasonalNaive(7, QS), df, df, origins, QS)
    merged = res.forecasts.merge(
        df[["origin_date", "target_time", "p_lag7d"]], on=["origin_date", "target_time"]
    )
    # The median residual offset is small but nonzero; check the point tracks the lag.
    assert np.corrcoef(merged["q50"], merged["p_lag7d"])[0, 1] > 0.99


def test_contract_violation_is_rejected() -> None:
    class Bad(Spy):
        def predict(self, origin: pd.Timestamp, features: pd.DataFrame) -> pd.DataFrame:
            return pd.DataFrame({"q50": 0.0}, index=features.index)

    df = synthetic_dataset()
    with pytest.raises(ValueError, match="contract"):
        run_backtest(Bad("monthly"), df, df, select_origins(df)[1:2], QS)  # type: ignore[arg-type]


def test_naive_weekday_uses_last_week_for_sat_sun_mon() -> None:
    df = synthetic_dataset()
    rows = df[df["origin_date"].isin(sorted(df["origin_date"].unique())[8:15])]
    model = SeasonalNaive("weekday", QS)
    point, _ = model._point(rows)
    expect = np.where(rows["cal_dow"].isin([0, 5, 6]), rows["p_lag7d"], rows["p_lag1d"])
    assert np.allclose(point, expect, equal_nan=True)
    assert model.name == "naive_weekday"


def test_weekly_dev_stride_is_rejected() -> None:
    from pathlib import Path

    from pydantic import ValidationError

    from pricefc.config import BacktestConfig, load_yaml

    raw = load_yaml(Path("configs/backtest.yaml"))
    with pytest.raises(ValidationError, match="multiple of 7"):
        BacktestConfig.model_validate({**raw, "dev_every_n_days": 14})


def test_model_spec_overrides() -> None:
    from pricefc.backtest.run import resolve_spec

    params = {"lightgbm": {"seed": 42, "n_estimators": 10}}
    name, p, ts = resolve_spec("lightgbm@seed=3@train_start=2023-07-01", params, None)
    assert (name, p["seed"], ts) == ("lightgbm", 3, date(2023, 7, 1))
    assert params["lightgbm"]["seed"] == 42  # the shared config is not mutated
    with pytest.raises(ValueError):
        resolve_spec("lightgbm@lr=0.1", params, None)
