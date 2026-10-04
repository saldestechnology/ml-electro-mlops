import numpy as np
import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.extra.numpy import arrays

from pricefc.backtest.metrics import (
    compute_metrics,
    daily_losses,
    pinball,
    qcol,
    slice_metrics,
    sort_quantiles,
)
from pricefc.backtest.stats import block_bootstrap_mean, block_indices, diebold_mariano

QS = [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95]
finite = st.floats(-1e4, 1e4, allow_nan=False, allow_infinity=False)
taus = st.floats(0.01, 0.99)


# --- pinball ----------------------------------------------------------------------------------


@given(
    arrays("float64", st.integers(1, 50), elements=finite),
    arrays("float64", st.integers(1, 50), elements=finite),
    taus,
)
def test_pinball_non_negative(y: np.ndarray, f: np.ndarray, tau: float) -> None:
    n = min(len(y), len(f))
    assert (pinball(y[:n], f[:n], tau) >= 0).all()


@given(arrays("float64", st.integers(1, 50), elements=finite), taus)
def test_pinball_zero_for_perfect_forecast(y: np.ndarray, tau: float) -> None:
    assert (pinball(y, y, tau) == 0).all()


@given(
    arrays("float64", st.integers(1, 50), elements=finite),
    arrays("float64", st.integers(1, 50), elements=finite),
    taus,
    st.floats(0.01, 100),
)
def test_pinball_scales_with_data(y: np.ndarray, f: np.ndarray, tau: float, c: float) -> None:
    n = min(len(y), len(f))
    a = pinball(c * y[:n], c * f[:n], tau)
    assert np.allclose(a, c * pinball(y[:n], f[:n], tau), rtol=1e-9, atol=1e-6)


def test_pinball_known_answer() -> None:
    # y above the 0.9 quantile by 10 costs 9; below by 10 costs 1.
    assert pinball(np.array([10.0]), np.array([0.0]), 0.9)[0] == pytest.approx(9.0)
    assert pinball(np.array([0.0]), np.array([10.0]), 0.9)[0] == pytest.approx(1.0)


@given(arrays("float64", st.tuples(st.integers(1, 30), st.just(len(QS))), elements=finite))
def test_sort_quantiles_makes_rows_non_crossing(vals: np.ndarray) -> None:
    df = pd.DataFrame(vals, columns=[qcol(q) for q in QS])
    fixed, _ = sort_quantiles(df, QS)
    assert (np.diff(fixed.to_numpy(), axis=1) >= 0).all()


def test_qcol() -> None:
    assert [qcol(q) for q in QS] == ["q05", "q10", "q25", "q50", "q75", "q90", "q95"]
    with pytest.raises(ValueError):
        qcol(0.025)


# --- headline metrics -------------------------------------------------------------------------


def forecast_frame(y: np.ndarray, spread: float) -> pd.DataFrame:
    ts = pd.date_range("2025-10-03", periods=len(y), freq="h", tz="UTC")
    df = pd.DataFrame(
        {
            "target_time": ts,
            "y": y,
            "target_date": ts.tz_convert("Europe/Stockholm").date.astype(str),
        }
    )
    z = {0.05: -1.645, 0.1: -1.2816, 0.25: -0.6745, 0.5: 0, 0.75: 0.6745, 0.9: 1.2816, 0.95: 1.645}
    for q in QS:
        df[qcol(q)] = 50 + z[q] * spread
    return df


def test_calibrated_gaussian_forecast_has_nominal_coverage() -> None:
    y = np.random.default_rng(0).normal(50, 10, 20_000)
    m = compute_metrics(forecast_frame(y, 10), QS)
    assert m["coverage_80"] == pytest.approx(0.80, abs=0.01)
    assert m["coverage_90"] == pytest.approx(0.90, abs=0.01)
    assert m["bias"] == pytest.approx(0, abs=0.2)
    # A sharper but miscalibrated forecast loses on pinball despite equal MAE.
    narrow = compute_metrics(forecast_frame(y, 1), QS)
    assert narrow["mae"] == pytest.approx(m["mae"])
    assert narrow["pinball_mean"] > m["pinball_mean"]


def test_daily_losses_and_slices() -> None:
    y = np.random.default_rng(1).normal(50, 10, 24 * 10)
    fc = forecast_frame(y, 10)
    daily = daily_losses(fc, QS)
    assert len(daily) == fc["target_date"].nunique()
    assert daily["pinball"].mean() == pytest.approx(
        compute_metrics(fc, QS)["pinball_mean"], rel=0.05
    )
    sl = slice_metrics(fc, QS, "Europe/Stockholm")
    assert set(sl["slice"]) == {"hour", "daytype", "season", "spike_day", "negative"}
    assert sl.loc[sl["slice"] == "hour", "n"].sum() == len(fc)


# --- bootstrap --------------------------------------------------------------------------------

series = arrays("float64", st.integers(3, 120), elements=st.floats(-100, 100))


@given(series, st.integers(1, 14), st.integers(0, 2**31 - 1))
def test_bootstrap_reproducible_with_seed(x: np.ndarray, block: int, seed: int) -> None:
    a = block_bootstrap_mean(x, block_length=block, n_resamples=200, seed=seed)
    b = block_bootstrap_mean(x, block_length=block, n_resamples=200, seed=seed)
    assert a == b


@given(st.integers(1, 200), st.integers(1, 30), st.integers(0, 2**31 - 1))
def test_bootstrap_indices_stay_in_series(n: int, block: int, seed: int) -> None:
    idx = block_indices(n, block, 50, np.random.default_rng(seed))
    assert idx.shape == (50, n) and idx.min() >= 0 and idx.max() < n


@given(series, st.integers(1, 14), st.integers(0, 2**31 - 1))
def test_bootstrap_estimate_inside_interval(x: np.ndarray, block: int, seed: int) -> None:
    r = block_bootstrap_mean(x, block_length=block, n_resamples=500, seed=seed)
    tol = 1e-9 * max(1.0, abs(r.estimate))
    assert r.lo - tol <= r.estimate <= r.hi + tol


# --- Diebold-Mariano ---------------------------------------------------------------------------


def test_dm_detects_clear_difference_and_not_noise() -> None:
    rng = np.random.default_rng(3)
    base = rng.gamma(2.0, 5.0, 365)
    better = base * 0.8
    r = diebold_mariano(better, base)
    assert r.statistic < 0 and r.p_value < 1e-6
    same = diebold_mariano(base + rng.normal(0, 1, 365), base + rng.normal(0, 1, 365))
    assert same.p_value > 0.01


def test_dm_is_antisymmetric() -> None:
    rng = np.random.default_rng(4)
    a, b = rng.gamma(2, 5, 100), rng.gamma(2, 5, 100)
    ab, ba = diebold_mariano(a, b), diebold_mariano(b, a)
    assert ab.statistic == pytest.approx(-ba.statistic)
    assert ab.p_value == pytest.approx(ba.p_value)
