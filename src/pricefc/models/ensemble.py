"""Quantile-averaging ensemble (spec section 8.4), itself a `Forecaster` in the harness.

At each origin the members are refit on their own cadence and predict as usual. The
ensemble forecast is a convex combination of the members' (sorted) quantiles. Weights are
chosen by minimising mean pinball loss over the members' *own past forecasts* of target days
that are published by the origin; realised prices reach the ensemble only through
`fit(train)`, i.e. the harness's published-before-origin rows, so it cannot see the future.

Weighting schemes:
  equal   fixed 1/K.
  global  one weight vector for all hours.
  hourly  one weight vector per local hour of the delivery day.
Weights are searched on a simplex grid (step `grid_step`). Until `min_days` of history exist,
weights are equal (cold start).

Member forecasts are memoised per process, keyed by zone, member spec, origin and row count,
so that several ensemble variants in one backtest run reuse the members' work. A process
handles one dataset version per zone (the CLI does), so the key is unambiguous. A served
model turns the memo off (`memoize = False`): its members must see every origin themselves,
or their own state (recalibration buffers) would miss it.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from typing import Any, Literal

import numpy as np
import pandas as pd

from pricefc.backtest.metrics import qcol
from pricefc.models.base import Forecaster, Refit

Weighting = Literal["equal", "global", "hourly"]

_MEMO: dict[tuple[Any, ...], np.ndarray] = {}


def simplex_grid(k: int, step: float) -> np.ndarray:
    """All weight vectors of length k on the simplex with the given step, shape (P, k)."""
    n = round(1 / step)
    pts = [c for c in itertools.product(range(n + 1), repeat=k - 1) if sum(c) <= n]
    return np.array([[*c, n - sum(c)] for c in pts], dtype=float) / n


def best_weights(
    member_q: np.ndarray, y: np.ndarray, taus: np.ndarray, grid: np.ndarray
) -> np.ndarray:
    """member_q: (K, n, Q) sorted quantiles; y: (n,). Returns the grid weights (K,) with the
    lowest mean pinball loss; ties go to the first (most balanced) candidate."""
    comb = np.tensordot(grid, member_q, axes=(1, 0))  # (P, n, Q)
    diff = y[None, :, None] - comb
    loss = np.maximum(taus * diff, (taus - 1) * diff).mean(axis=(1, 2))
    best: np.ndarray = grid[int(np.argmin(np.round(loss, 12)))]
    return best


def _balanced_first(grid: np.ndarray) -> np.ndarray:
    spread = grid.max(axis=1) - grid.min(axis=1)
    return grid[np.argsort(spread, kind="stable")]


class EnsembleForecaster:
    family = "ensemble"
    refit: Refit = "every_origin"  # weights update daily; members keep their own cadence

    def __init__(
        self,
        members: Sequence[Forecaster],
        member_specs: Sequence[str],
        quantiles: Sequence[float],
        *,
        name: str = "ensemble",
        weighting: Weighting = "hourly",
        window_days: int | None = None,
        min_days: int = 14,
        grid_step: float = 0.05,
    ) -> None:
        if len(members) < 2 or len(members) != len(member_specs):
            raise ValueError("an ensemble needs at least two members, each with a spec")
        self.members = list(members)
        self.member_specs = list(member_specs)
        self.quantiles = list(quantiles)
        self.taus = np.asarray(self.quantiles)
        self.name = name
        self.weighting = weighting
        self.window_days = window_days
        self.min_days = min_days
        self.grid_step = grid_step
        self._grid = _balanced_first(simplex_grid(len(members), grid_step))
        self._equal = np.full(len(members), 1 / len(members))
        self._weights: dict[int, np.ndarray] = {}  # group -> weights; -1 = all hours
        self._last_fit: list[tuple[int, int] | None] = [None] * len(members)
        self._train: pd.DataFrame | None = None
        self._day: str | None = None
        self._history: list[pd.DataFrame] = []
        self.weight_log: list[dict[str, Any]] = []
        self.memoize = True
        self.refit_members = True  # False: predict from the members' current state only

    def __getstate__(self) -> dict[str, Any]:
        # The training frame is re-supplied by fit() before every predict; not state.
        state = self.__dict__.copy()
        state["_train"] = None
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        state.setdefault("memoize", True)
        state.setdefault("refit_members", True)
        self.__dict__.update(state)

    # -- harness interface ---------------------------------------------------------------

    def fit(self, train: pd.DataFrame) -> None:
        self._train = train
        self._day = str(train["target_date"].max())
        self._update_weights(train)

    def predict(self, origin: pd.Timestamp, features: pd.DataFrame) -> pd.DataFrame:
        stack = np.stack(
            [self._member_forecast(i, origin, features) for i in range(len(self.members))]
        )
        hours = features["cal_hour"].to_numpy(dtype=int)
        w = np.stack([self._weights_for(h) for h in hours], axis=1)  # (K, n)
        combined = np.einsum("kn,knq->nq", w, stack)
        self._history.append(
            pd.DataFrame(
                {
                    "target_time": features["target_time"].to_numpy(),
                    "target_date": features["target_date"].to_numpy(),
                    "cal_hour": hours,
                    "q": list(np.transpose(stack, (1, 0, 2))),  # per row: (K, Q)
                }
            )
        )
        return pd.DataFrame(
            combined, index=features.index, columns=[qcol(t) for t in self.quantiles]
        )

    def params(self) -> dict[str, Any]:
        return {
            "members": ",".join(self.member_specs),
            "weighting": self.weighting,
            "window_days": self.window_days if self.window_days else "expanding",
            "min_days": self.min_days,
            "grid_step": self.grid_step,
        }

    def version_info(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for spec, m in zip(self.member_specs, self.members, strict=True):
            safe = spec.replace("@", "__").replace("=", "-")  # MLflow param-name rules
            out.update({f"{safe}.{k}": v for k, v in m.version_info().items()})
        return out

    def run_tags(self) -> dict[str, str]:
        tags: list[dict[str, str]] = [getattr(m, "run_tags", dict)() for m in self.members]
        deployable = all(t.get("deployable", "true") == "true" for t in tags)
        licences = sorted({t["model_licence"] for t in tags if "model_licence" in t})
        out = {"deployable": str(deployable).lower()}
        if licences:
            out["model_licence"] = ",".join(licences)
        return out

    # -- internals -----------------------------------------------------------------------

    def _member_forecast(self, i: int, origin: pd.Timestamp, features: pd.DataFrame) -> np.ndarray:
        key = (
            str(features["zone"].iloc[0]),
            self.member_specs[i],
            str(origin),
            len(features),
            tuple(self.quantiles),
        )
        if not self.memoize or key not in _MEMO:
            member = self.members[i]
            assert self._day is not None
            day = pd.Timestamp(self._day)
            month = (day.year, day.month)
            due = member.refit == "every_origin" or self._last_fit[i] != month
            if due and self.refit_members:
                assert self._train is not None
                member.fit(self._train)
                self._last_fit[i] = month
            pred = member.predict(origin, features)
            q = np.sort(pred[[qcol(t) for t in self.quantiles]].to_numpy(dtype="float64"), axis=1)
            if not self.memoize:
                return q
            _MEMO[key] = q
        return _MEMO[key]

    def _weights_for(self, hour: int) -> np.ndarray:
        if self.weighting == "hourly":
            return self._weights.get(hour, self._equal)
        return self._weights.get(-1, self._equal)

    def _update_weights(self, train: pd.DataFrame) -> None:
        if self.weighting == "equal" or not self._history:
            return
        hist = pd.concat(self._history, ignore_index=True)
        last = pd.Timestamp(train["target_date"].max())
        mask = hist["target_date"].astype(str) <= last.date().isoformat()
        if self.window_days:
            start = (last - pd.Timedelta(days=self.window_days - 1)).date().isoformat()
            mask &= hist["target_date"].astype(str) >= start
        hist = hist[mask].merge(train[["target_time", "y"]], on="target_time", how="inner")
        days = hist["target_date"].nunique()
        if days < self.min_days:
            return
        if self.weighting == "global":
            groups = [(-1, hist)]
        else:
            groups = [(int(str(h)), part) for h, part in hist.groupby("cal_hour")]
        for g, part in groups:
            q = np.transpose(np.stack(part["q"].to_list()), (1, 0, 2))  # (K, n, Q)
            self._weights[g] = best_weights(
                q, part["y"].to_numpy(dtype="float64"), self.taus, self._grid
            )
        self.weight_log.append(
            {
                "last_target_date": last.date().isoformat(),
                "days": days,
                **{
                    f"w_{g}_{spec}": float(w[k])
                    for g, w in self._weights.items()
                    for k, spec in enumerate(self.member_specs)
                },
            }
        )


def build_ensemble(
    name: str,
    params: dict[str, Any],
    *,
    zone: str,
    quantiles: Sequence[float],
    model_params: dict[str, dict[str, Any]],
) -> EnsembleForecaster:
    """Members are model specs (variants allowed) with an optional `{zone}` placeholder."""
    from pricefc.backtest.run import resolve_spec
    from pricefc.models.baselines import build_model

    params = dict(params)
    specs = [s.format(zone=zone) for s in params.pop("members")]
    members = []
    for spec in specs:
        mname, mparams, train_start = resolve_spec(spec, model_params, None)
        if train_start is not None:
            raise ValueError("ensemble members share the ensemble's training window")
        member = build_model(mname, quantiles, mparams)
        member.name = spec
        members.append(member)
    return EnsembleForecaster(members, specs, quantiles, name=name, **params)
