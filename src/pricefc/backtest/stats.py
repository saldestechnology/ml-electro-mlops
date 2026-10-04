"""Statistical comparison of forecasts (spec section 9.2).

Both tools work on *daily* loss series in time order, never on shuffled rows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from scipy import stats


@dataclass(frozen=True)
class BootstrapResult:
    estimate: float
    lo: float
    hi: float
    n: int
    n_resamples: int
    block_length: int
    level: float

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


def block_indices(
    n: int, block_length: int, n_resamples: int, rng: np.random.Generator
) -> np.ndarray:
    """Circular block bootstrap indices, shape (n_resamples, n).

    Blocks of consecutive days keep the short-range dependence of daily losses. Wrapping
    around the end gives every day the same chance of selection.
    """
    if n < 1:
        raise ValueError("empty series")
    length = max(1, min(block_length, n))
    n_blocks = -(-n // length)
    starts = rng.integers(0, n, size=(n_resamples, n_blocks))
    idx = (starts[:, :, None] + np.arange(length)[None, None, :]) % n
    return idx.reshape(n_resamples, -1)[:, :n]


def block_bootstrap_mean(
    x: np.ndarray,
    *,
    block_length: int,
    n_resamples: int,
    seed: int,
    level: float = 0.95,
) -> BootstrapResult:
    """Percentile CI for the mean of a daily series (or of paired differences)."""
    x = np.asarray(x, dtype="float64")
    if np.isnan(x).any():
        raise ValueError("series contains NaN")
    rng = np.random.default_rng(seed)
    idx = block_indices(len(x), block_length, n_resamples, rng)
    means = x[idx].mean(axis=1)
    alpha = (1 - level) / 2
    lo, hi = np.quantile(means, [alpha, 1 - alpha])
    return BootstrapResult(
        float(x.mean()), float(lo), float(hi), len(x), n_resamples, min(block_length, len(x)), level
    )


@dataclass(frozen=True)
class DMResult:
    statistic: float
    p_value: float
    mean_diff: float
    n: int
    horizon: int

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


def diebold_mariano(loss_a: np.ndarray, loss_b: np.ndarray, horizon: int = 1) -> DMResult:
    """Diebold-Mariano test with the Harvey-Leybourne-Newbold small-sample correction.

    H0: equal expected loss. Negative statistic means `a` has lower loss. Two-sided p-value
    from Student t with n-1 degrees of freedom. `horizon` sets the autocovariance lags used
    (h-1); daily losses of a day-ahead forecast use h=1.
    """
    d = np.asarray(loss_a, dtype="float64") - np.asarray(loss_b, dtype="float64")
    n = len(d)
    if n < 3:
        raise ValueError("need at least 3 paired observations")
    mean = d.mean()
    centred = d - mean
    var = np.dot(centred, centred) / n
    for k in range(1, horizon):
        var += 2 * np.dot(centred[k:], centred[:-k]) / n
    if var <= 0:
        return DMResult(float("nan"), float("nan"), float(mean), n, horizon)
    dm = mean / np.sqrt(var / n)
    correction = np.sqrt((n + 1 - 2 * horizon + horizon * (horizon - 1) / n) / n)
    stat = float(dm * correction)
    p = float(2 * stats.t.sf(abs(stat), df=n - 1))
    return DMResult(stat, p, float(mean), n, horizon)
