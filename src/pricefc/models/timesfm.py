"""TimesFM foundation-model forecasters (spec section 8.3), zero-shot, optionally with covariates.

The forecaster is a thin adapter: `fit(train)` only stores the published price history (and
covariates) from the harness's training rows, and `predict` feeds the most recent
`context_hours` of it to a pretrained checkpoint. Like MSTL it refits at every origin, so the
context always ends at the last published hour (end of local day D).

Checkpoints are pinned by Hugging Face repo id and revision. TimesFM 3.0 weights carry a
non-commercial licence; such models are tagged `deployable=false` and must be explicitly
enabled with `accept_noncommercial_licence: true`.

TimesFM emits deciles (0.1..0.9). Other quantiles are interpolated between deciles; the tails
(below 0.1, above 0.9) are extrapolated from the median assuming a normal shape. Recalibration
(`calibration_window_days`) can correct the tails afterwards.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import pandas as pd
from scipy.stats import norm

from pricefc.backtest.metrics import qcol
from pricefc.models.base import Refit

DECILES = np.round(np.arange(0.1, 0.91, 0.1), 2)


def _single_threaded_torch() -> None:
    """LightGBM and torch ship separate libomp copies on macOS; with both thread pools in
    one process, torch inference deadlocks after a LightGBM fit (reproduced 2026-10-04).
    One torch thread avoids it at little cost for batch-of-one inference (0.7 s/origin)."""
    import torch

    torch.set_num_threads(1)


@dataclass(frozen=True)
class Checkpoint:
    repo_id: str
    licence: str
    deployable: bool


CHECKPOINTS = {
    "2.5": Checkpoint("google/timesfm-2.5-200m-pytorch", "apache-2.0", deployable=True),
    "3.0": Checkpoint(
        "google/timesfm-3.0-pytorch", "timesfm-non-commercial-license-v1.0", deployable=False
    ),
}


def deciles_to_quantiles(deciles: np.ndarray, quantiles: Sequence[float]) -> np.ndarray:
    """Map (..., 9) decile forecasts to (..., len(quantiles)).

    Inside [0.1, 0.9]: linear interpolation in tau. Outside: the distance from the median to
    the outermost decile, scaled by the ratio of normal z-scores.
    """
    d = np.sort(deciles, axis=-1)
    med = d[..., 4]
    out = []
    for tau in quantiles:
        if DECILES[0] <= tau <= DECILES[-1]:
            i = int(np.clip(np.searchsorted(DECILES, tau) - 1, 0, len(DECILES) - 2))
            w = (tau - DECILES[i]) / (DECILES[i + 1] - DECILES[i])
            out.append(d[..., i] * (1 - w) + d[..., i + 1] * w)
        else:
            edge = 0 if tau < DECILES[0] else -1
            scale = norm.ppf(tau) / norm.ppf(DECILES[edge])
            out.append(med + (d[..., edge] - med) * scale)
    return np.stack(out, axis=-1)


class Backend(Protocol):
    def forecast(
        self, contexts: list[np.ndarray], horizon: int, covariates: list[np.ndarray] | None
    ) -> np.ndarray:
        """contexts[i]: (context,), covariates[i]: (n_cov, context + horizon).
        Returns deciles of shape (n_series, horizon, 9)."""
        ...


class _TimesFM25:
    def __init__(self, revision: str, max_context: int, covariates: bool) -> None:
        import timesfm

        _single_threaded_torch()

        self.model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
            CHECKPOINTS["2.5"].repo_id, revision=revision, torch_compile=False
        )
        self.model.compile(
            timesfm.ForecastConfig(
                max_context=max_context,
                max_horizon=128,
                normalize_inputs=True,
                use_continuous_quantile_head=True,
                fix_quantile_crossing=True,
                infer_is_positive=False,  # prices can be negative
                return_backcast=covariates,  # required by XReg
                per_core_batch_size=8,
            )
        )

    def forecast(
        self, contexts: list[np.ndarray], horizon: int, covariates: list[np.ndarray] | None
    ) -> np.ndarray:
        if covariates is None:
            _, q = self.model.forecast(horizon=horizon, inputs=list(contexts))
            return np.asarray(q)[:, :horizon, 1:10]
        dyn = {f"c{j}": [c[j] for c in covariates] for j in range(covariates[0].shape[0])}
        _, qs = self.model.forecast_with_covariates(
            inputs=list(contexts),
            dynamic_numerical_covariates=dyn,
            xreg_mode="xreg + timesfm",
            normalize_xreg_target_per_input=True,
            ridge=1.0,
            force_on_cpu=True,
        )
        return np.stack([np.asarray(q)[:horizon, 1:10] for q in qs])


class _TimesFM3:
    def __init__(self, revision: str, backend: str, device: str | None) -> None:
        backend = _resolve_timesfm3_backend(backend)
        if backend == "mlx":
            from timesfm3.mlx import TimesFM3Forecaster as MlxForecaster

            self.model: Any = MlxForecaster.from_pretrained(
                CHECKPOINTS["3.0"].repo_id, revision=revision
            )
        else:
            from timesfm3 import TimesFM3Forecaster

            _single_threaded_torch()

            self.model = TimesFM3Forecaster.from_pretrained(
                CHECKPOINTS["3.0"].repo_id, revision=revision, device=device
            )

    def forecast(
        self, contexts: list[np.ndarray], horizon: int, covariates: list[np.ndarray] | None
    ) -> np.ndarray:
        outs = self.model.predict_batch(
            contexts=list(contexts),
            horizon=horizon,
            past_future_covariates=list(covariates) if covariates is not None else None,
            return_quantiles=True,
        )
        return np.stack([np.asarray(o.quantiles) for o in outs])


def _resolve_timesfm3_backend(backend: str, *, system: str | None = None) -> str:
    """Resolve portable `auto` state to MLX on supported Macs and torch elsewhere."""
    if backend not in {"auto", "mlx", "torch"}:
        raise ValueError(f"unknown TimesFM 3.0 backend {backend!r}")
    if backend != "auto":
        return backend
    host = system or sys.platform
    if host == "darwin" and importlib.util.find_spec("mlx") is not None:
        return "mlx"
    return "torch"


_BACKENDS: dict[tuple[Any, ...], Backend] = {}


def release_backends() -> None:
    """Drop every loaded checkpoint (the next forecast loads its own again)."""
    import gc

    _BACKENDS.clear()
    gc.collect()


def load_backend(
    version: str,
    revision: str,
    *,
    backend: str,
    max_context: int,
    covariates: bool,
    device: str | None = None,
) -> Backend:
    """Load (once per process) a pinned checkpoint."""
    key = (version, revision, backend, max_context, covariates, device)
    if key not in _BACKENDS:
        if version == "2.5":
            _BACKENDS[key] = _TimesFM25(revision, max_context, covariates)
        elif version == "3.0":
            _BACKENDS[key] = _TimesFM3(revision, backend, device)
        else:
            raise KeyError(f"unknown TimesFM version {version!r}")
    return _BACKENDS[key]


class TimesFMForecaster:
    family = "timesfm"
    refit: Refit = "every_origin"  # fit only stores history; the context must be current

    def __init__(
        self,
        quantiles: Sequence[float],
        *,
        name: str,
        version: str,
        revision: str,
        context_hours: int = 2048,
        covariates: Sequence[str] = (),
        backend: str = "torch",
        device: str | None = None,
        accept_noncommercial_licence: bool = False,
        backend_impl: Backend | None = None,
    ) -> None:
        if version not in CHECKPOINTS:
            raise KeyError(f"unknown TimesFM version {version!r}")
        self.checkpoint = CHECKPOINTS[version]
        if not self.checkpoint.deployable and not accept_noncommercial_licence:
            raise PermissionError(
                f"{self.checkpoint.repo_id} is licensed {self.checkpoint.licence} "
                "(non-commercial, non-production); set accept_noncommercial_licence: true "
                "to use it as a research benchmark"
            )
        if len(revision) != 40:
            raise ValueError("pin the checkpoint by its full 40-character revision hash")
        self.quantiles = list(quantiles)
        self.name = name
        self.version = version
        self.revision = revision
        self.context_hours = context_hours
        self.covariates = list(covariates)
        self.backend = backend
        self.device = device
        self._impl = backend_impl
        self._impl_injected = backend_impl is not None
        self._history: pd.Series | None = None
        self._cov_history: pd.DataFrame | None = None
        self.interpolated = 0

    def __getstate__(self) -> dict[str, Any]:
        # A loaded checkpoint is process state (cached by load_backend), not model state.
        state = self.__dict__.copy()
        if not self._impl_injected:
            state["_impl"] = None
        return state

    def _backend(self) -> Backend:
        if self._impl is None:
            self._impl = load_backend(
                self.version,
                self.revision,
                backend=self.backend,
                max_context=self.context_hours,
                covariates=bool(self.covariates),
                device=self.device,
            )
        return self._impl

    def fit(self, train: pd.DataFrame) -> None:
        frame = train.drop_duplicates("target_time").set_index("target_time").sort_index()
        end = frame.index.max()
        grid = pd.date_range(end - pd.Timedelta(hours=self.context_hours - 1), end, freq="h")
        y = frame["y"].reindex(grid)
        # Excluded source days leave gaps; the model needs a regular series, so they are
        # interpolated here only, as model input, and counted.
        self.interpolated += int(y.isna().sum())
        self._history = y.interpolate(limit_direction="both")
        if self.covariates:
            missing = set(self.covariates) - set(frame.columns)
            if missing:
                raise KeyError(f"covariates not in dataset: {sorted(missing)}")
            self._cov_history = frame[self.covariates].reindex(grid)

    def predict(self, origin: pd.Timestamp, features: pd.DataFrame) -> pd.DataFrame:
        if self._history is None:
            raise RuntimeError("fit() first")
        hist = self._history
        targets = pd.DatetimeIndex(features["target_time"])
        if targets.min() <= hist.index.max():
            raise ValueError("forecast targets overlap the context")
        future = pd.date_range(hist.index.max() + pd.Timedelta(hours=1), targets.max(), freq="h")
        covs = None
        if self.covariates:
            assert self._cov_history is not None
            fut = features.set_index("target_time")[self.covariates].reindex(future)
            both = pd.concat([self._cov_history, fut]).astype("float64")
            both = both.interpolate(limit_direction="both").fillna(0.0)
            covs = [both.to_numpy().T.astype("float32")]
        deciles = self._backend().forecast([hist.to_numpy(dtype="float32")], len(future), covs)[0]
        q = pd.DataFrame(
            deciles_to_quantiles(deciles, self.quantiles),
            index=future,
            columns=[qcol(t) for t in self.quantiles],
        ).reindex(targets)
        q.index = features.index
        return q

    def params(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "checkpoint": self.checkpoint.repo_id,
            "revision": self.revision,
            "context_hours": self.context_hours,
            "covariates": ",".join(self.covariates),
            "backend": (
                _resolve_timesfm3_backend(self.backend) if self.version == "3.0" else self.backend
            ),
            "quantile_mapping": "decile_interp_normal_tails",
        }

    def version_info(self) -> dict[str, Any]:
        try:
            pkg = importlib.metadata.version("timesfm")
        except importlib.metadata.PackageNotFoundError:
            pkg = "not-installed"
        return {"timesfm": pkg, "checkpoint_revision": self.revision}

    def run_tags(self) -> dict[str, str]:
        return {
            "model_licence": self.checkpoint.licence,
            "deployable": str(self.checkpoint.deployable).lower(),
        }
