"""A served model and its state between origins.

The champion is stateful: LightGBM refits monthly, recalibration keeps its recent raw
forecasts, the ensemble learns its weights from the members' past forecasts and TimesFM
needs the latest context. `ModelState` holds the model together with what the backtest
harness tracks (last fit, last origin) and advances it with the harness's own `step`, one
origin at a time and in order. Saved after every origin and loaded for the next, a served
model therefore produces exactly the forecasts of an uninterrupted backtest.

Missed origins (a day the forecast flow did not run) are replayed in order by `catch_up`,
from the evaluation dataset, before the new origin is forecast.

States are pickles: load them only from our own MLflow registry or disk.
"""

from __future__ import annotations

import contextlib
import copy
import importlib.metadata
import json
import os
import pickle
import platform
from collections.abc import Iterator, Sequence
from dataclasses import MISSING, dataclass, field, fields
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from pricefc.backtest.harness import FitRecord, finish_forecasts, step
from pricefc.backtest.metrics import quantile_columns
from pricefc.models.base import Forecaster
from pricefc.models.calibration import RecalibratedForecaster
from pricefc.models.ensemble import EnsembleForecaster

FORMAT_VERSION = 1
STATE_FILE = "state.pkl"
META_FILE = "state.json"
_PACKAGES = ("pricefc", "lightgbm", "timesfm", "torch", "numpy", "pandas", "scikit-learn")


class StateError(RuntimeError):
    """The state cannot be advanced as asked (wrong order, missing data, wrong zone)."""


def _walk(model: Any) -> Iterator[Any]:
    yield model
    if isinstance(model, RecalibratedForecaster):
        yield from _walk(model.base)
    elif isinstance(model, EnsembleForecaster):
        for m in model.members:
            yield from _walk(m)


def _versions() -> dict[str, str]:
    out = {"python": platform.python_version()}
    for pkg in _PACKAGES:
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            out[pkg] = importlib.metadata.version(pkg)
    return out


@dataclass
class ModelState:
    model: Forecaster
    zone: str
    spec: str
    quantiles: list[float]
    train_start: date | None = None
    first_origin: date | None = None  # a fresh state starts here (the backtest's eval_start)
    last_fit: date | None = None
    last_origin: date | None = None
    fits: list[FitRecord] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)  # one entry per origin
    datasets: dict[str, str] = field(default_factory=dict)  # versions of the last advance
    source_version: str | None = None  # registry version this state was pulled from

    def __setstate__(self, state: dict[str, Any]) -> None:
        # States saved before a field existed get its default.
        for f in fields(self):
            if f.name not in state and f.default_factory is not MISSING:
                state[f.name] = f.default_factory()
            elif f.name not in state:
                state[f.name] = f.default
        self.__dict__.update(state)

    def __post_init__(self) -> None:
        for m in _walk(self.model):
            if isinstance(m, EnsembleForecaster):
                m.memoize = False

    # -- advancing -----------------------------------------------------------------------

    def pending_origins(self, eval_df: pd.DataFrame, until: date) -> list[date]:
        """Origins in `eval_df` after the last one served, up to and including `until`."""
        days = sorted(date.fromisoformat(d) for d in eval_df["origin_date"].unique())
        if self.last_origin is not None:
            days = [d for d in days if d > self.last_origin]
        elif self.first_origin is not None:
            days = [d for d in days if d >= self.first_origin]
        return [d for d in days if d <= until]

    def advance(self, train_df: pd.DataFrame, eval_df: pd.DataFrame, until: date) -> pd.DataFrame:
        """Step through every pending origin up to `until` (catch-up first, in order) and
        return the forecasts of all of them; the last origin must be `until` itself."""
        if self.last_origin is not None and until <= self.last_origin:
            raise StateError(f"{self.zone}: origin {until} is not after {self.last_origin}")
        days = self.pending_origins(eval_df, until)
        if not days or days[-1] != until:
            raise StateError(f"{self.zone}: no evaluation rows for origin {until}")
        if set(eval_df["zone"].unique()) != {self.zone}:
            raise StateError(f"evaluation rows are not all for zone {self.zone}")
        cols = quantile_columns(self.quantiles)
        by_origin = {k: v for k, v in eval_df.groupby("origin_date")}
        train_dates = pd.to_datetime(train_df["target_date"]).dt.date.to_numpy()
        frames = []
        for day in days:
            res = step(
                self.model,
                train_df,
                by_origin[day.isoformat()],
                day,
                self.last_fit,
                cols,
                train_dates=train_dates,
                train_start=self.train_start,
            )
            if res.fit is not None:
                self.fits.append(res.fit)
                self.last_fit = day
            self.last_origin = day
            self.history.append(
                {
                    "origin_date": day.isoformat(),
                    "refit": res.fit is not None,
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
            )
            frames.append(res.frame)
        forecasts, _ = finish_forecasts(self.spec, frames, self.quantiles)
        return forecasts

    def preview(self, rows: pd.DataFrame) -> pd.DataFrame:
        """Forecast `rows` from the current state without the day's update (no refit, no
        new prices), on a copy: the state itself is unchanged. For the rows of the last origin
        served this reproduces the served forecast exactly; for a later origin it is the
        forecast of a model that has not seen the newest day."""
        model = copy.deepcopy(self.model)
        for m in _walk(model):
            if isinstance(m, EnsembleForecaster):
                m.refit_members = False
        if rows["origin_date"].nunique() != 1:
            raise StateError("preview one origin at a time")
        pred = model.predict(
            pd.Timestamp(rows["origin"].iloc[0]),
            rows.drop(columns=["y", "y_n_periods", "y_is_pt15m"], errors="ignore"),
        )
        out = rows[["origin_date", "origin", "target_time", "target_date"]].join(pred)
        forecasts, _ = finish_forecasts(self.spec, [out], self.quantiles)
        return forecasts

    # -- persistence ---------------------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "zone": self.zone,
            "spec": self.spec,
            "quantiles": self.quantiles,
            "train_start": self.train_start.isoformat() if self.train_start else None,
            "first_origin": self.first_origin.isoformat() if self.first_origin else None,
            "last_fit": self.last_fit.isoformat() if self.last_fit else None,
            "last_origin": self.last_origin.isoformat() if self.last_origin else None,
            "origins_served": len(self.history),
            "datasets": self.datasets,
            "source_version": self.source_version,
            "git_sha": os.environ.get("PRICEFC_GIT_SHA", "unknown"),
            "saved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "versions": _versions(),
        }

    def save(self, directory: Path) -> Path:
        """Write `state.pkl` and a readable `state.json` to `directory` (atomically)."""
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".{STATE_FILE}.tmp"
        with tmp.open("wb") as f:
            pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(directory / STATE_FILE)
        (directory / META_FILE).write_text(json.dumps(self.metadata(), indent=2))
        return directory

    @classmethod
    def load(cls, directory: Path) -> ModelState:
        meta = json.loads((directory / META_FILE).read_text())
        if meta["format_version"] != FORMAT_VERSION:
            raise StateError(f"state format {meta['format_version']}, expected {FORMAT_VERSION}")
        with (directory / STATE_FILE).open("rb") as f:
            state = pickle.load(f)  # our own artifact only, see module docstring
        if not isinstance(state, cls):
            raise StateError(f"{directory} does not hold a ModelState")
        state.__post_init__()
        return state


def new_state(
    spec: str,
    zone: str,
    base: Any,
    model_params: dict[str, dict[str, Any]],
    *,
    first_origin: date | None = None,
    default_train_start: date | None = None,
    quantiles: Sequence[float] | None = None,
) -> ModelState:
    """A fresh (never fitted) state for a model spec, built exactly as the backtest builds it."""
    from pricefc.backtest.run import build_forecaster

    model, _, train_start = build_forecaster(spec, base, model_params, zone, default_train_start)
    return ModelState(
        model, zone, spec, list(quantiles or base.quantiles), train_start, first_origin
    )
