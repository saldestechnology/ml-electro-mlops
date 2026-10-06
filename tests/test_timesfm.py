"""TimesFM adapter tests with a fake backend (no torch/checkpoint needed)."""

from types import ModuleType
from typing import Any

import numpy as np
import pandas as pd
import pytest

from pricefc.backtest.harness import run_backtest, select_origins
from pricefc.backtest.metrics import qcol
from pricefc.models.baselines import build_model
from pricefc.models.timesfm import (
    DECILES,
    TimesFMForecaster,
    _resolve_timesfm3_backend,
    _TimesFM3,
    deciles_to_quantiles,
)
from tests.test_harness import QS, synthetic_dataset

REV = "0" * 40
COVS = ["w_a_temperature_2m", "cal_hour_sin"]


class FakeBackend:
    """Forecasts the last context value plus a fixed decile fan; records its inputs."""

    def __init__(self) -> None:
        self.calls: list[tuple[np.ndarray, int, list[np.ndarray] | None]] = []

    def forecast(
        self, contexts: list[np.ndarray], horizon: int, covariates: list[np.ndarray] | None
    ) -> np.ndarray:
        self.calls.append((contexts[0].copy(), horizon, covariates))
        fan = (DECILES - 0.5) * 10
        return np.stack([np.tile(c[-1] + fan, (horizon, 1)) for c in contexts])


def model(covariates: list[str] | None = None, context_hours: int = 200) -> TimesFMForecaster:
    return TimesFMForecaster(
        QS,
        name="timesfm_test",
        version="2.5",
        revision=REV,
        context_hours=context_hours,
        covariates=covariates or [],
        backend_impl=FakeBackend(),
    )


def split(df: pd.DataFrame, day: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    return df[df["target_date"] <= day], df[df["origin_date"] == day].drop(columns="y")


def test_deciles_map_exactly_and_tails_extend_monotonically() -> None:
    d = np.array([[1, 2, 3, 4, 5, 6, 7, 8, 9]], dtype=float)
    q = deciles_to_quantiles(d, [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95])[0]
    assert q[1] == 1 and q[3] == 5 and q[5] == 9
    assert q[2] == pytest.approx(2.5) and q[4] == pytest.approx(7.5)
    assert q[0] < 1 and q[6] > 9 and np.all(np.diff(q) > 0)


def test_context_ends_right_before_the_targets_and_uses_only_training_rows() -> None:
    df = synthetic_dataset()
    train, feats = split(df, "2025-04-01")
    m = model()
    m.fit(train)
    out = m.predict(pd.Timestamp(feats["origin"].iloc[0]), feats)
    ctx, horizon, _ = m._impl.calls[0]  # type: ignore[union-attr]
    hist = train.drop_duplicates("target_time").set_index("target_time")["y"].sort_index()
    assert len(ctx) == 200
    np.testing.assert_allclose(ctx, hist.to_numpy()[-200:], rtol=1e-6)
    assert hist.index[-1] + pd.Timedelta(hours=1) == feats["target_time"].min()
    assert horizon == len(feats)
    assert list(out.columns) == [qcol(q) for q in QS] and out.index.equals(feats.index)
    assert out[qcol(0.5)].iloc[0] == pytest.approx(float(ctx[-1]))


def test_covariates_span_context_and_horizon_with_future_values_from_features() -> None:
    df = synthetic_dataset()
    train, feats = split(df, "2025-04-01")
    m = model(COVS)
    m.fit(train)
    m.predict(pd.Timestamp(feats["origin"].iloc[0]), feats)
    _, horizon, covs = m._impl.calls[0]  # type: ignore[union-attr]
    assert covs is not None and covs[0].shape == (len(COVS), 200 + horizon)
    np.testing.assert_allclose(covs[0][:, 200:], feats[COVS].to_numpy().T, rtol=1e-6)


def test_gaps_in_history_are_interpolated_and_counted() -> None:
    df = synthetic_dataset()
    train, feats = split(df, "2025-04-01")
    m = model()
    m.fit(train[train["target_date"] != "2025-03-28"])  # an excluded source day
    assert m.interpolated == 24
    m.predict(pd.Timestamp(feats["origin"].iloc[0]), feats)
    ctx, _, _ = m._impl.calls[0]  # type: ignore[union-attr]
    assert not np.isnan(ctx).any()


def test_runs_in_the_harness_across_the_dst_change() -> None:
    df = synthetic_dataset()
    origins = select_origins(df, every_n_days=1)[2:]
    res = run_backtest(model(COVS), df, df, origins, QS)
    hours = res.forecasts.groupby("origin_date").size()
    assert set(hours) == {23, 24}  # 2025-03-30 has 23 hours
    assert res.forecasts[qcol(0.5)].notna().all()


def test_noncommercial_checkpoint_requires_explicit_acceptance() -> None:
    with pytest.raises(PermissionError):
        TimesFMForecaster(QS, name="t", version="3.0", revision=REV)
    m = TimesFMForecaster(
        QS, name="t", version="3.0", revision=REV, accept_noncommercial_licence=True
    )
    assert m.run_tags()["deployable"] == "false"
    assert model().run_tags()["deployable"] == "true"


def test_revision_must_be_pinned() -> None:
    with pytest.raises(ValueError, match="revision"):
        TimesFMForecaster(QS, name="t", version="2.5", revision="main")


def test_build_model_and_calibration_wrapper_forward_tags() -> None:
    params = {
        "version": "3.0",
        "revision": REV,
        "accept_noncommercial_licence": True,
        "calibration_window_days": 28,
    }
    m = build_model("timesfm3", QS, params)  # must not load weights eagerly
    assert m.run_tags()["deployable"] == "false"


def test_timesfm3_auto_backend_uses_torch_on_linux_without_mlx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pricefc.models.timesfm.importlib.util.find_spec", lambda _: None)
    assert _resolve_timesfm3_backend("auto", system="linux") == "torch"
    assert _resolve_timesfm3_backend("auto", system="darwin") == "torch"
    monkeypatch.setattr("pricefc.models.timesfm.importlib.util.find_spec", lambda _: object())
    assert _resolve_timesfm3_backend("auto", system="darwin") == "mlx"
    assert _resolve_timesfm3_backend("torch", system="darwin") == "torch"


def test_timesfm3_adapter_loads_torch_backend_without_importing_mlx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str | None]] = []

    class FakeForecaster:
        @classmethod
        def from_pretrained(cls, _repo: str, *, revision: str, device: str | None) -> Any:
            calls.append({"revision": revision, "device": device})
            return cls()

    fake_timesfm3 = ModuleType("timesfm3")
    fake_timesfm3.TimesFM3Forecaster = FakeForecaster  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "timesfm3", fake_timesfm3)
    monkeypatch.setattr("pricefc.models.timesfm.importlib.util.find_spec", lambda _: None)
    monkeypatch.setattr("pricefc.models.timesfm._single_threaded_torch", lambda: None)

    adapter = _TimesFM3(REV, "auto", None)

    assert isinstance(adapter.model, FakeForecaster)
    assert calls == [{"revision": REV, "device": None}]


def test_challenger_spec_uses_only_configured_tfm3_covariates() -> None:
    from pricefc.config import load_model_params

    params = load_model_params(__import__("pathlib").Path("configs/models"))
    spec = params["ensemble_hourly_exp_tfm3"]
    assert spec == {
        "members": ["lightgbm_tuned_{zone}@calibration=28", "timesfm3_cov"],
        "weighting": "hourly",
        "window_days": None,
    }
    covariates = params["timesfm3_cov"]["covariates"]
    assert set(covariates) <= set(synthetic_dataset().columns)
