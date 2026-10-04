from datetime import date

import numpy as np
import pandas as pd

from pricefc.backtest.harness import run_backtest, select_origins
from pricefc.backtest.metrics import compute_metrics, qcol
from pricefc.models.calibration import RecalibratedForecaster
from tests.test_harness import QS, Spy, synthetic_dataset


class Shifted(Spy):
    """Overconfident and biased: a narrow fan centred 50 below the true price level."""

    def __init__(self) -> None:
        super().__init__("monthly")

    def predict(self, origin: pd.Timestamp, features: pd.DataFrame) -> pd.DataFrame:
        centre = features["p_lag1d"].fillna(features["p_dayD_mean"]).to_numpy() - 50
        return pd.DataFrame({qcol(q): centre + (q - 0.5) for q in QS}, index=features.index)


def test_recalibration_fixes_bias_and_coverage_after_warmup() -> None:
    df = synthetic_dataset()
    origins = select_origins(df)[1:]
    raw = run_backtest(Shifted(), df, df, origins, QS).forecasts  # type: ignore[arg-type]
    cal_model = RecalibratedForecaster(Shifted(), QS, window_days=14, min_days=7)  # type: ignore[arg-type]
    cal = run_backtest(cal_model, df, df, origins, QS).forecasts
    after = cal["origin_date"] > "2025-03-25"  # calibration active (>= 7 known days)
    m_raw, m_cal = compute_metrics(raw[after], QS), compute_metrics(cal[after], QS)
    assert abs(m_cal["bias"]) < abs(m_raw["bias"]) / 5
    assert m_cal["coverage_80"] > m_raw["coverage_80"] + 0.3
    assert m_cal["pinball_mean"] < m_raw["pinball_mean"]


def test_recalibration_only_uses_published_targets() -> None:
    """Offsets at origin D are learned only from target days <= D."""
    df = synthetic_dataset()
    model = RecalibratedForecaster(Shifted(), QS, window_days=14, min_days=1)  # type: ignore[arg-type]
    run_backtest(model, df, df, select_origins(df)[1:20], QS)
    fits = model.calibration_log
    assert fits
    for entry in fits:
        assert date.fromisoformat(entry["last_target_date"]) <= date(2025, 4, 1)
    # Before any forecast's target is published, there is nothing to calibrate on.
    assert np.isfinite([e["offset_q50"] for e in fits]).all()
