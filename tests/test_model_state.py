"""A served model, saved and loaded between origins, must reproduce its backtest exactly."""

import pickle
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from pricefc.backtest.harness import run_backtest, select_origins
from pricefc.models.calibration import RecalibratedForecaster
from pricefc.models.ensemble import EnsembleForecaster
from pricefc.models.lgbm import LGBMQuantile
from pricefc.models.timesfm import TimesFMForecaster
from pricefc.serving.state import ModelState, StateError
from tests.test_harness import QS, synthetic_dataset
from tests.test_timesfm import FakeBackend

REV = "0" * 40


def champion_like() -> RecalibratedForecaster | EnsembleForecaster:
    """Same structure as ensemble_hourly_exp: recalibrated LightGBM (monthly refit) +
    TimesFM, hourly weights over an expanding window; small windows for a short dataset."""
    lgbm = LGBMQuantile(QS, params={"min_child_samples": 5, "num_leaves": 7}, n_estimators=20)
    members = [
        RecalibratedForecaster(lgbm, QS, window_days=7, min_days=3),
        TimesFMForecaster(
            QS,
            name="tfm",
            version="2.5",
            revision=REV,
            context_hours=96,
            backend_impl=FakeBackend(),
        ),
    ]
    return EnsembleForecaster(
        members,
        ["lgbm@calibration=7", "tfm"],
        QS,
        name="ens",
        weighting="hourly",
        window_days=None,
        min_days=3,
    )


@pytest.fixture(scope="module")
def data() -> tuple[pd.DataFrame, list[date]]:
    df = synthetic_dataset()
    # The first origin has no published targets in this one-frame setup; real runs train on
    # a longer history.
    return df, select_origins(df)[2:]


@pytest.fixture(scope="module")
def reference(data: tuple[pd.DataFrame, list[date]]) -> tuple[pd.DataFrame, EnsembleForecaster]:
    df, origins = data
    model = champion_like()
    res = run_backtest(model, df, df, origins, QS)
    assert isinstance(model, EnsembleForecaster)
    # The scenario exercises all state: a monthly LightGBM refit (March, April), learnt
    # recalibration offsets and learnt ensemble weights.
    cal = model.members[0]
    assert isinstance(cal, RecalibratedForecaster) and isinstance(cal.base, LGBMQuantile)
    assert len(cal.base.importance_history) == 2
    assert cal.calibration_log and model.weight_log
    return res.forecasts, model


def live_state(first: date) -> ModelState:
    return ModelState(champion_like(), "SE3", "ens", QS, first_origin=first)


def test_saved_and_reloaded_every_origin_matches_backtest(
    data: tuple[pd.DataFrame, list[date]],
    reference: tuple[pd.DataFrame, EnsembleForecaster],
    tmp_path: Path,
) -> None:
    df, origins = data
    expected, ref_model = reference
    state = live_state(origins[0])
    frames = []
    for day in origins:
        state.save(tmp_path)
        state = ModelState.load(tmp_path)
        frames.append(state.advance(df, df.drop(columns="y"), day))
    got = pd.concat(frames, ignore_index=True)
    pd.testing.assert_frame_equal(got, expected.drop(columns="y"))
    # Members saw every origin themselves (no memo shortcut): same internal state.
    live = state.model
    assert isinstance(live, EnsembleForecaster)
    assert live.weight_log == ref_model.weight_log
    live_cal, ref_cal = live.members[0], ref_model.members[0]
    assert isinstance(live_cal, RecalibratedForecaster)
    assert isinstance(ref_cal, RecalibratedForecaster)
    assert live_cal.calibration_log == ref_cal.calibration_log
    assert state.last_origin == origins[-1]
    assert len(state.history) == len(origins)


def test_catch_up_replays_missed_origins_in_order(
    data: tuple[pd.DataFrame, list[date]],
    reference: tuple[pd.DataFrame, EnsembleForecaster],
    tmp_path: Path,
) -> None:
    df, origins = data
    expected, _ = reference
    state = live_state(origins[0])
    first = state.advance(df, df, origins[10])  # cold start: everything up to origin 10
    state.save(tmp_path)
    rest = ModelState.load(tmp_path).advance(df, df, origins[-1])  # missed days, then today
    got = pd.concat([first, rest], ignore_index=True)
    pd.testing.assert_frame_equal(got, expected)
    assert rest["origin_date"].nunique() == len(origins) - 11


def test_refuses_going_back_or_missing_origin(data: tuple[pd.DataFrame, list[date]]) -> None:
    df, origins = data
    state = live_state(origins[0])
    state.advance(df, df, origins[3])
    with pytest.raises(StateError, match="not after"):
        state.advance(df, df, origins[3])
    without = df[df["origin_date"] != origins[5].isoformat()]
    with pytest.raises(StateError, match="no evaluation rows"):
        state.advance(df, without, origins[5])
    with pytest.raises(StateError, match="zone"):
        state.advance(df, df.assign(zone="SE4"), origins[4])


def test_pickle_drops_training_frame_and_loaded_checkpoint(
    data: tuple[pd.DataFrame, list[date]],
) -> None:
    df, origins = data
    state = live_state(origins[0])
    state.advance(df, df, origins[3])
    ens = state.model
    assert isinstance(ens, EnsembleForecaster) and ens._train is not None
    tfm = TimesFMForecaster(QS, name="tfm", version="2.5", revision=REV)
    tfm._impl = lambda: None  # type: ignore[assignment]  # stands in for a loaded checkpoint
    ens.members[1] = tfm
    loaded = pickle.loads(pickle.dumps(state))
    assert loaded.model._train is None
    assert loaded.model.members[1]._impl is None
    assert loaded.model.memoize is False
