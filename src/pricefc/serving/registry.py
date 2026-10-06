"""The served model in the MLflow model registry.

A registered version of `se-price-{zone}-hourly` is a model *and its state at one origin*:
code (git SHA), configuration (model spec) and the fitted state after `last_origin`,
packaged as a pyfunc so MLflow can load it. The alias `champion` marks the version that the
forecast flow serves. The daily state then lives on the environment's disk and is advanced
from there; a new champion version (alias moved) is pulled and caught up from its own
`last_origin`, so promotion never skips an origin.

The pyfunc's `predict` is `ModelState.preview`: a forecast from the stored state without the
day's update. For the rows of the version's last origin it reproduces the served forecast
exactly, which makes every registered version checkable.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import mlflow
import pandas as pd
from mlflow.exceptions import MlflowException
from mlflow.pyfunc.model import PythonModel

from pricefc.config import BaseConfig
from pricefc.serving.state import META_FILE, ModelState

CHAMPION = "champion"
STATE_ARTIFACT = "state"


class AliasNotFoundError(LookupError):
    """The registry has no model or version assigned to the requested alias."""


def model_name(zone: str) -> str:
    return f"se-price-{zone.lower()}-hourly"


class StateModel(PythonModel):
    def load_context(self, context: Any) -> None:
        self.state = ModelState.load(Path(context.artifacts[STATE_ARTIFACT]))

    def predict(
        self, context: Any, model_input: pd.DataFrame, params: dict[str, Any] | None = None
    ) -> pd.DataFrame:
        return self.state.preview(model_input)


def register_state(
    base: BaseConfig,
    state: ModelState,
    state_dir: Path,
    *,
    alias: str | None = None,
    description: str | None = None,
) -> str:
    """Log the saved state in `state_dir` as a pyfunc model, register it as a new version of
    `model_name(zone)` and optionally point `alias` at it. Returns the version number."""
    from pricefc.tracking.mlflow_utils import base_tags, setup_tracking, start_run

    if not (state_dir / META_FILE).exists():
        raise FileNotFoundError(f"no saved state in {state_dir}")
    setup_tracking(base)
    tags = base_tags(
        base,
        zone=state.zone,
        pipeline_stage="register",
        model_family=getattr(state.model, "family", "unknown"),
        dataset_version=state.datasets.get("eval", "none"),
    )
    tags.update({"model_spec": state.spec, "last_origin": str(state.last_origin)})
    tags.update(getattr(state.model, "run_tags", dict)())
    if tags.get("deployable", "true") != "true" and base.licence_policy != "noncommercial_ok":
        raise PermissionError(
            f"{state.spec} is not deployable ({tags.get('model_licence')}); "
            "licence_policy is deployable_only"
        )
    name = model_name(state.zone)
    run_name = f"{state.zone}-{state.spec}-{state.last_origin}"
    with start_run("training", tags, base, run_name=run_name):
        mlflow.log_params(
            {
                "model": state.spec,
                "last_origin": str(state.last_origin),
                "last_fit": str(state.last_fit),
                "first_origin": str(state.first_origin),
                "origins_served": len(state.history),
                **{f"dataset.{k}": v for k, v in state.datasets.items()},
            }
        )
        info = mlflow.pyfunc.log_model(
            name="model",
            python_model=StateModel(),
            artifacts={STATE_ARTIFACT: str(state_dir)},
            pip_requirements=[f"mlflow=={mlflow.__version__}"],
            registered_model_name=name,
        )
    version = str(info.registered_model_version)
    client = mlflow.MlflowClient()
    for k in (
        "model_spec",
        "last_origin",
        "git_sha",
        "git_dirty",
        "deployable",
        "model_licence",
    ):
        if k in tags:
            client.set_model_version_tag(name, version, k, tags[k])
    if description:
        client.update_model_version(name, version, description=description)
    if alias:
        client.set_registered_model_alias(name, alias, version)
    return version


def resolve(zone: str, alias: str = CHAMPION, *, tracking_uri: str | None = None) -> str:
    """The version number `alias` points at."""
    if tracking_uri is not None:
        mlflow.set_tracking_uri(tracking_uri)
    try:
        mv = mlflow.MlflowClient().get_model_version_by_alias(model_name(zone), alias)
    except MlflowException as exc:
        if exc.error_code == "RESOURCE_DOES_NOT_EXIST":
            raise AliasNotFoundError(f"no {alias!r} alias registered for {zone}") from exc
        raise
    return str(mv.version)


def resolve_optional(zone: str, alias: str, *, tracking_uri: str | None = None) -> str | None:
    """Return an alias version, or `None` when the alias has not been registered."""
    try:
        return resolve(zone, alias, tracking_uri=tracking_uri)
    except AliasNotFoundError:
        return None


def pull_state(zone: str, version: str, dest: Path) -> ModelState:
    """Download a registered version's state into `dest` (replacing it) and load it."""
    import shutil

    with tempfile.TemporaryDirectory() as tmp:
        local = Path(
            mlflow.artifacts.download_artifacts(
                artifact_uri=f"models:/{model_name(zone)}/{version}", dst_path=tmp
            )
        )
        # The artifact keeps the source directory's name; MLmodel records where it is.
        flavor = mlflow.models.Model.load(local).flavors["python_function"]
        src = local / flavor["artifacts"][STATE_ARTIFACT]["path"]
        state = ModelState.load(src)
        if state.zone != zone:
            raise ValueError(f"version {version} holds zone {state.zone}, not {zone}")
        state.source_version = version
        if dest.exists():
            shutil.rmtree(dest)
        state.save(dest)
    return state
