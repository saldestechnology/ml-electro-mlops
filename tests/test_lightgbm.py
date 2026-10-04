from datetime import date, timedelta
from itertools import pairwise

import numpy as np
import pandas as pd
from hypothesis import given
from hypothesis import strategies as st

from pricefc.datasets.build import META_COLUMNS
from pricefc.models.lgbm import LGBMQuantile
from pricefc.models.tuning import (
    CVConfig,
    HPOConfig,
    SpaceParam,
    StudyConfig,
    final_n_estimators,
    make_folds,
    run_study,
)
from tests.test_harness import QS, synthetic_dataset


@given(
    st.dates(date(2022, 1, 1), date(2030, 1, 1)),
    st.integers(2, 8),
    st.integers(7, 120),
    st.integers(0, 7),
)
def test_folds_are_ordered_expanding_and_before_eval(
    eval_start: date, n: int, val_days: int, gap: int
) -> None:
    folds = make_folds(eval_start, n, val_days, gap)
    assert len(folds) == n
    assert folds[-1].val_end == eval_start - timedelta(days=1)
    for f in folds:
        assert f.train_end < f.val_start  # training ends before validation
        assert (f.val_start - f.train_end).days == gap + 1  # the gap is respected
        assert (f.val_end - f.val_start).days == val_days - 1
    for a, b in pairwise(folds):
        assert b.val_start == a.val_end + timedelta(days=1)  # consecutive, no overlap
        assert b.train_end > a.train_end  # expanding window


def test_lightgbm_contract_and_determinism() -> None:
    df = synthetic_dataset()
    train, test = df[df["origin_date"] < "2025-04-10"], df[df["origin_date"] == "2025-04-10"]
    origin = pd.Timestamp(test["origin"].iloc[0])

    def fitted(seed: int) -> pd.DataFrame:
        m = LGBMQuantile(QS, {"num_leaves": 7, "min_child_samples": 5}, n_estimators=30, seed=seed)
        m.fit(train)
        assert not set(m.features) & {*META_COLUMNS, "y"}
        imp = m.feature_importance()
        assert np.isclose(imp["gain_share"].sum(), 1.0)
        return m.predict(origin, test)

    a, b = fitted(1), fitted(1)
    assert list(a.columns) == ["q05", "q10", "q25", "q50", "q75", "q90", "q95"]
    assert a.index.equals(test.index)
    pd.testing.assert_frame_equal(a, b)  # same seed, same forecasts


def test_tiny_study_runs_and_never_validates_on_eval_window() -> None:
    df = synthetic_dataset()
    eval_start = date(2025, 4, 10)
    cfg = HPOConfig(
        study=StudyConfig(n_trials=3, timeout_s=60, seed=0),
        cv=CVConfig(n_folds=2, val_days=7, gap_days=1),
        tune_quantiles=[0.5],
        max_estimators=40,
        early_stopping_rounds=5,
        fixed={"subsample_freq": 1},
        space={
            "num_leaves": SpaceParam(low=4, high=16, int=True),
            "learning_rate": SpaceParam(low=0.05, high=0.3, log=True),
        },
    )
    study = run_study(df, eval_start, cfg, study_name="t", storage="sqlite:///:memory:")
    assert len(study.trials) == 3
    for f in study.user_attrs["folds"]:
        assert date.fromisoformat(f["val_end"]) < eval_start
    assert 10 <= final_n_estimators(study) <= 40
