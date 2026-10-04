"""Forecast metrics (spec section 9.1).

A forecast table has one row per (origin_date, target_time) with the realised price `y` and
one column per quantile (`q05`, `q10`, ..., `q95`). The point forecast is `q50`.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

# sMAPE is unstable near zero prices; the denominator is floored at this many EUR/MWh.
SMAPE_FLOOR = 1.0


def qcol(tau: float) -> str:
    """Column name for a quantile level: 0.05 -> 'q05', 0.5 -> 'q50'."""
    pct = round(tau * 100, 6)
    if pct != int(pct):
        raise ValueError(f"quantile {tau} is not a whole percentage")
    return f"q{int(pct):02d}"


def quantile_columns(quantiles: Sequence[float]) -> list[str]:
    return [qcol(q) for q in quantiles]


def pinball(y: np.ndarray, pred: np.ndarray, tau: float) -> np.ndarray:
    """Elementwise pinball (quantile) loss."""
    diff = y - pred
    return np.asarray(np.maximum(tau * diff, (tau - 1.0) * diff), dtype="float64")


def sort_quantiles(df: pd.DataFrame, quantiles: Sequence[float]) -> tuple[pd.DataFrame, int]:
    """Fix quantile crossing by sorting each row. Returns the frame and rows changed."""
    cols = quantile_columns(quantiles)
    vals = df[cols].to_numpy(dtype="float64")
    fixed = np.sort(vals, axis=1)
    changed = int((~np.isclose(vals, fixed, equal_nan=True)).any(axis=1).sum())
    out = df.copy()
    out[cols] = fixed
    return out, changed


def point_metrics(y: np.ndarray, f: np.ndarray) -> dict[str, float]:
    err = f - y
    denom = np.maximum(np.abs(y) + np.abs(f), SMAPE_FLOOR)
    return {
        "mae": float(np.mean(np.abs(err))),
        "rmse": float(np.sqrt(np.mean(err**2))),
        "bias": float(np.mean(err)),
        "smape": float(np.mean(2 * np.abs(err) / denom)),
    }


def interval_metrics(
    y: np.ndarray, lo: np.ndarray, hi: np.ndarray, nominal: int
) -> dict[str, float]:
    return {
        f"coverage_{nominal}": float(np.mean((y >= lo) & (y <= hi))),
        f"width_{nominal}": float(np.mean(hi - lo)),
    }


def compute_metrics(fc: pd.DataFrame, quantiles: Sequence[float]) -> dict[str, float]:
    """All headline metrics for a forecast table (rows with missing y are ignored)."""
    fc = fc[fc["y"].notna()]
    y = fc["y"].to_numpy(dtype="float64")
    out: dict[str, float] = {"n": float(len(fc))}
    if not len(fc):
        return out
    out.update(point_metrics(y, fc["q50"].to_numpy(dtype="float64")))
    losses = []
    for tau in quantiles:
        loss = float(np.mean(pinball(y, fc[qcol(tau)].to_numpy(dtype="float64"), tau)))
        out[f"pinball_{qcol(tau)}"] = loss
        losses.append(loss)
    out["pinball_mean"] = float(np.mean(losses))
    # Quantile-score approximation of CRPS on the available quantile grid.
    out["crps_approx"] = 2.0 * out["pinball_mean"]
    for lo_tau, hi_tau, nominal in ((0.1, 0.9, 80), (0.05, 0.95, 90)):
        if lo_tau in quantiles and hi_tau in quantiles:
            out.update(
                interval_metrics(
                    y,
                    fc[qcol(lo_tau)].to_numpy(dtype="float64"),
                    fc[qcol(hi_tau)].to_numpy(dtype="float64"),
                    nominal,
                )
            )
    return out


def daily_losses(fc: pd.DataFrame, quantiles: Sequence[float]) -> pd.DataFrame:
    """Per target day: mean pinball over quantiles and hours, and MAE of q50.

    Daily series are the unit for the block bootstrap and the Diebold-Mariano test.
    """
    fc = fc[fc["y"].notna()]
    y = fc["y"].to_numpy(dtype="float64")
    pin = np.mean([pinball(y, fc[qcol(t)].to_numpy(dtype="float64"), t) for t in quantiles], axis=0)
    frame = pd.DataFrame(
        {
            "target_date": fc["target_date"].to_numpy(),
            "pinball": pin,
            "ae": np.abs(fc["q50"].to_numpy(dtype="float64") - y),
        }
    )
    return frame.groupby("target_date").mean().sort_index()


# --- slices ------------------------------------------------------------------------------------

SEASONS = {
    12: "winter",
    1: "winter",
    2: "winter",
    3: "spring",
    4: "spring",
    5: "spring",
    6: "summer",
    7: "summer",
    8: "summer",
    9: "autumn",
    10: "autumn",
    11: "autumn",
}


def add_slice_columns(fc: pd.DataFrame, tz: str, spike_quantile: float = 0.95) -> pd.DataFrame:
    """Columns used to slice metrics: local hour, day type, season, spike day, negative hour.

    Spike days are target days whose mean realised price is in the top
    `1 - spike_quantile` share of days in this table (i.e. top 5% by default).
    """
    out = fc.copy()
    local = pd.DatetimeIndex(out["target_time"]).tz_convert(tz)
    out["slice_hour"] = local.hour
    out["slice_daytype"] = np.where(local.dayofweek >= 5, "weekend", "weekday")
    out["slice_season"] = [SEASONS[m] for m in local.month]
    day_mean = out.groupby("target_date")["y"].transform("mean")
    threshold = out.groupby("target_date")["y"].mean().quantile(spike_quantile)
    out["slice_spike_day"] = np.where(day_mean >= threshold, "spike", "normal")
    out["slice_negative"] = np.where(out["y"] < 0, "negative", "non_negative")
    return out


def slice_metrics(fc: pd.DataFrame, quantiles: Sequence[float], tz: str) -> pd.DataFrame:
    """Long table: slice, group, metric columns."""
    sliced = add_slice_columns(fc, tz)
    rows = []
    for col in ("slice_hour", "slice_daytype", "slice_season", "slice_spike_day", "slice_negative"):
        for group, part in sliced.groupby(col):
            rows.append(
                {
                    "slice": col.removeprefix("slice_"),
                    "group": str(group),
                    **compute_metrics(part, quantiles),
                }
            )
    return pd.DataFrame(rows)
