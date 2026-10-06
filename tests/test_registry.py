"""Registering a served state and pulling it back must not change a single forecast."""

from datetime import date
from pathlib import Path

import mlflow
import pandas as pd
import pytest

from pricefc.config import BaseConfig, load_config
from pricefc.serving.registry import model_name, pull_state, register_state, resolve
from pricefc.serving.state import ModelState
from tests.test_harness import QS, synthetic_dataset
from tests.test_model_state import champion_like

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


@pytest.fixture
def cfg(tmp_path: Path) -> BaseConfig:
    return load_config(
        Path("configs/base.yaml"),
        {
            "quantiles": QS,
            "mlflow": {
                "tracking_uri": f"sqlite:///{tmp_path}/m.db",
                "artifact_root": str(tmp_path / "art"),
            },
        },
    )


def served(tmp_path: Path) -> tuple[ModelState, pd.DataFrame, list[date], pd.DataFrame]:
    df = synthetic_dataset()
    origins = sorted(date.fromisoformat(d) for d in df["origin_date"].unique())[2:]
    state = ModelState(champion_like(), "SE3", "ens", QS, first_origin=origins[0])
    fc = state.advance(df, df, origins[8])
    state.datasets = {"train": "t1", "eval": "e1"}
    state.save(tmp_path / "state")
    return state, df, origins, fc


def test_register_pull_and_continue_exactly(cfg: BaseConfig, tmp_path: Path) -> None:
    state, df, origins, served_fc = served(tmp_path)
    version = register_state(cfg, state, tmp_path / "state", alias="champion")
    assert resolve("SE3") == version == "1"
    mv = mlflow.MlflowClient().get_model_version(model_name("SE3"), version)
    assert mv.tags["last_origin"] == str(origins[8]) and mv.tags["model_spec"] == "ens"

    pulled = pull_state("SE3", version, tmp_path / "live")
    assert pulled.source_version == "1" and pulled.last_origin == origins[8]
    assert ModelState.load(tmp_path / "live").source_version == "1"
    # The pulled state continues exactly like the one that was registered.
    expected = state.advance(df, df, origins[12])
    pd.testing.assert_frame_equal(pulled.advance(df, df, origins[12]), expected)

    # The pyfunc reproduces the version's last served forecast.
    model = mlflow.pyfunc.load_model(f"models:/{model_name('SE3')}@champion")
    rows = df[df["origin_date"] == origins[8].isoformat()]
    last = served_fc[served_fc["origin_date"] == origins[8].isoformat()].reset_index(drop=True)
    got = model.predict(rows).reset_index(drop=True)
    pd.testing.assert_frame_equal(got, last.drop(columns="y"))


def test_preview_leaves_state_unchanged(tmp_path: Path) -> None:
    state, df, origins, _ = served(tmp_path)
    twin = ModelState.load(tmp_path / "state")
    state.preview(df[df["origin_date"] == origins[9].isoformat()])
    pd.testing.assert_frame_equal(
        state.advance(df, df, origins[10]), twin.advance(df, df, origins[10])
    )


def test_non_deployable_model_is_refused(cfg: BaseConfig, tmp_path: Path) -> None:
    state, *_ = served(tmp_path)
    state.model.run_tags = lambda: {"deployable": "false", "model_licence": "nc"}  # type: ignore[attr-defined]
    with pytest.raises(PermissionError, match="not deployable"):
        register_state(cfg, state, tmp_path / "state")
