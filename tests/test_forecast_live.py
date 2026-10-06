"""The morning forecast step: pull the champion, catch up, forecast, back up, re-run safely."""

import sys
import types
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import mlflow
import pandas as pd
import pytest

from pricefc.backtest import run as bt_run
from pricefc.config import BaseConfig, load_config
from pricefc.datasets.registry import StoredDataset
from pricefc.serving import live as serving_live
from pricefc.serving.live import backups_dir, forecast_origin, forecast_path, state_dir
from pricefc.serving.registry import register_state
from pricefc.serving.state import META_FILE, STATE_FILE, ModelState, StateError
from tests.test_harness import QS, synthetic_dataset
from tests.test_model_state import champion_like

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")

MODEL = "ensemble_hourly_exp"
CHALLENGER = "ensemble_hourly_exp_tfm3"
TARGETS = ["y", "y_n_periods", "y_is_pt15m"]


@pytest.fixture
def cfg(tmp_path: Path) -> BaseConfig:
    data = tmp_path / "data"
    return load_config(
        Path("configs/base.yaml"),
        {
            "quantiles": QS,
            "paths": {"data_root": str(data), "raw": str(data / "raw"), "datasets": str(data)},
            "mlflow": {
                "tracking_uri": f"sqlite:///{tmp_path}/m.db",
                "artifact_root": str(tmp_path / "art"),
            },
        },
    )


@pytest.fixture
def setup(
    cfg: BaseConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[pd.DataFrame, list[date], Path]:
    """Champion v1 served up to origins[8]; latest datasets are the synthetic frame."""
    df = synthetic_dataset()
    origins = sorted(date.fromisoformat(d) for d in df["origin_date"].unique())[2:]
    state = ModelState(champion_like(), "SE3", MODEL, QS, first_origin=origins[0])
    state.advance(df, df, origins[8])
    state.datasets = {"train": "t0", "eval": "e0"}
    reg = tmp_path / "reg" / "v1"
    state.save(reg)
    register_state(cfg, state, reg, alias="champion")

    stored = {}
    for kind in ("stitched", "true_lead"):
        path = tmp_path / "ds" / kind
        path.mkdir(parents=True)
        df.to_parquet(path / "data.parquet", index=False)
        stored[kind] = StoredDataset(path, f"{kind}-v1", {"weather_kind": kind})

    def latest(base: Any, zone: str, kind: str, version: str | None = None) -> StoredDataset:
        assert zone == "SE3"
        return stored[kind]

    monkeypatch.setattr(bt_run, "latest_dataset", latest)
    return df, origins, reg


def live_rows(df: pd.DataFrame, day: date) -> pd.DataFrame:
    """What the live builder returns before D+1 prices exist: the origin's rows, no target."""
    rows = df[df["origin_date"] == day.isoformat()].reset_index(drop=True)
    return rows.assign(**{c: float("nan") for c in TARGETS})


def by_hand(reg: Path, df: pd.DataFrame, day: date) -> pd.DataFrame:
    state = ModelState.load(reg)
    combined = pd.concat(
        [df[df["origin_date"] < day.isoformat()], live_rows(df, day)], ignore_index=True
    )
    return state.advance(df, combined, day)


def run(cfg: BaseConfig, df: pd.DataFrame, day: date, **kw: Any) -> serving_live.ForecastResult:
    kw.setdefault("live", live_rows(df, day))
    return forecast_origin(cfg, "SE3", day, model=MODEL, **kw)


def test_first_run_pulls_and_forecasts_like_the_state_itself(
    cfg: BaseConfig, setup: tuple[pd.DataFrame, list[date], Path]
) -> None:
    df, origins, reg = setup
    local = state_dir(cfg, "SE3", MODEL)
    ModelState.load(reg).save(local)  # rsync'd copy: same content, no source version
    assert ModelState.load(local).source_version is None

    day = origins[9]
    res = run(cfg, df, day)
    assert res.model_version == "1" and res.caught_up == [] and res.origin_date == day
    got = res.forecasts.drop(columns=["model_version", "forecast_made_at"])
    pd.testing.assert_frame_equal(got, by_hand(reg, df, day))
    assert res.forecasts["y"].isna().all() and len(res.forecasts) == 24

    state = ModelState.load(local)
    assert state.source_version == "1" and state.last_origin == day
    assert state.datasets == {"train": "stitched-v1", "eval": "true_lead-v1", "live": str(day)}
    written = pd.read_parquet(forecast_path(cfg, "SE3", day))
    assert res.path == forecast_path(cfg, "SE3", day)
    assert (written["model_version"] == "1").all()
    assert str(written["forecast_made_at"].dt.tz) == "UTC"
    assert [p.name for p in backups_dir(local).iterdir()] == [origins[8].isoformat()]

    runs = mlflow.search_runs(experiment_names=["forecast-live"], output_format="list")
    assert len(runs) == 1
    r = runs[0].data
    assert r.tags["origin_date"] == str(day) and r.tags["model_version"] == "1"
    assert r.tags["pipeline_stage"] == "forecast" and r.tags["dataset.eval"] == "true_lead-v1"
    assert r.metrics["n_rows"] == 24 and r.metrics["n_origins_served"] == 1


def test_rerun_is_idempotent_and_missed_days_are_caught_up(
    cfg: BaseConfig, setup: tuple[pd.DataFrame, list[date], Path]
) -> None:
    df, origins, reg = setup
    local = state_dir(cfg, "SE3", MODEL)
    first = run(cfg, df, origins[9])
    saved = (local / STATE_FILE).read_bytes()

    again = run(cfg, df, origins[9], live=df.iloc[:0])  # live rows are not even looked at
    assert (local / STATE_FILE).read_bytes() == saved
    pd.testing.assert_frame_equal(again.forecasts, first.forecasts)

    res = run(cfg, df, origins[11])  # origins[10] was skipped
    assert res.caught_up == [origins[10]]
    assert forecast_path(cfg, "SE3", origins[10]).exists()
    both = pd.concat(
        [pd.read_parquet(forecast_path(cfg, "SE3", d)) for d in origins[10:12]], ignore_index=True
    )
    # Equal to one uninterrupted advance over 9..11 (the skipped day from the eval rows).
    expected = by_hand(reg, df, origins[11])
    expected = expected[expected["origin_date"] >= origins[10].isoformat()]
    pd.testing.assert_frame_equal(
        both.drop(columns=["model_version", "forecast_made_at"]),
        expected.reset_index(drop=True),
    )
    assert ModelState.load(local).last_origin == origins[11]

    with pytest.raises(StateError, match="never written"):
        run(cfg, df, origins[5])


def test_moved_alias_is_pulled_and_continued(
    cfg: BaseConfig, setup: tuple[pd.DataFrame, list[date], Path], tmp_path: Path
) -> None:
    df, origins, reg = setup
    run(cfg, df, origins[9])
    newer = ModelState.load(reg)
    newer.advance(df, df, origins[12])
    newer.save(tmp_path / "reg" / "v2")
    assert register_state(cfg, newer, tmp_path / "reg" / "v2", alias="champion") == "2"

    res = run(cfg, df, origins[13])
    assert res.model_version == "2" and res.caught_up == []
    state = ModelState.load(state_dir(cfg, "SE3", MODEL))
    assert state.source_version == "2" and state.last_origin == origins[13]
    # The replaced local state (after origins[9]) is kept as a backup.
    assert (backups_dir(state_dir(cfg, "SE3", MODEL)) / origins[9].isoformat()).exists()


def test_backups_are_pruned_and_live_rows_built_when_not_given(
    cfg: BaseConfig,
    setup: tuple[pd.DataFrame, list[date], Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    df, origins, _ = setup
    calls: list[date] = []

    def build_live_rows(base: Any, features: Any, ingest: Any, zone: str, day: date) -> Any:
        calls.append(day)
        return types.SimpleNamespace(frame=live_rows(df, day), path=tmp_path / str(day))

    fake = types.ModuleType("pricefc.datasets.live")
    fake.build_live_rows = build_live_rows  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pricefc.datasets.live", fake)

    for day in origins[9:13]:
        forecast_origin(cfg, "SE3", day, model=MODEL, keep_backups=2)
    assert calls == origins[9:13]
    kept = sorted(p.name for p in backups_dir(state_dir(cfg, "SE3", MODEL)).iterdir())
    assert kept == [origins[10].isoformat(), origins[11].isoformat()]
    restored = ModelState.load(backups_dir(state_dir(cfg, "SE3", MODEL)) / kept[-1])
    assert restored.last_origin == origins[11]
    assert ModelState.load(state_dir(cfg, "SE3", MODEL)).datasets["live"] == str(origins[12])


def test_column_mismatch_fails_before_the_state_is_touched(
    cfg: BaseConfig, setup: tuple[pd.DataFrame, list[date], Path]
) -> None:
    df, origins, _ = setup
    run(cfg, df, origins[9])
    local = state_dir(cfg, "SE3", MODEL)
    before = {f: (local / f).read_bytes() for f in (STATE_FILE, META_FILE)}
    bad = live_rows(df, origins[10]).drop(columns="cal_hour")
    with pytest.raises(ValueError, match="live columns differ"):
        run(cfg, df, origins[10], live=bad)
    assert {f: (local / f).read_bytes() for f in (STATE_FILE, META_FILE)} == before
    assert [p.name for p in backups_dir(local).iterdir()] == [origins[8].isoformat()]
    assert not forecast_path(cfg, "SE3", origins[10]).exists()


def test_missed_day_absent_from_eval_dataset_is_refused(
    cfg: BaseConfig, setup: tuple[pd.DataFrame, list[date], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missed origin the eval dataset does not hold yet would be skipped for good."""
    df, origins, _ = setup
    run(cfg, df, origins[9])
    gap = origins[10].isoformat()
    stale = df[df["origin_date"] != gap]
    path = Path(bt_run.latest_dataset(cfg, "SE3", "true_lead").path)
    stale.to_parquet(path / "data.parquet", index=False)
    local = state_dir(cfg, "SE3", MODEL)
    before = (local / STATE_FILE).read_bytes()
    with pytest.raises(StateError, match=f"lacks origins \\['{gap}'\\]"):
        run(cfg, stale, origins[11])
    assert (local / STATE_FILE).read_bytes() == before

    # A day the build dropped for lack of a target is skipped, as in the backtest.
    stored = bt_run.latest_dataset(cfg, "SE3", "true_lead")
    target = (origins[10] + timedelta(days=1)).isoformat()
    monkeypatch.setitem(stored.manifest, "build", {"target_dates_dropped": [target]})
    res = run(cfg, stale, origins[11])
    assert res.caught_up == []


def test_challenger_forecast_uses_separate_state_file_and_run_role(
    cfg: BaseConfig,
    setup: tuple[pd.DataFrame, list[date], Path],
    tmp_path: Path,
) -> None:
    df, origins, _ = setup
    state = ModelState(champion_like(), "SE3", CHALLENGER, QS, first_origin=origins[0])
    state.advance(df, df, origins[8])
    state.datasets = {"train": "stitched-v1", "eval": "true_lead-v1"}
    registration_dir = tmp_path / "challenger-registration"
    state.save(registration_dir)
    register_state(cfg, state, registration_dir, alias="challenger")

    day = origins[9]
    res = forecast_origin(
        cfg,
        "SE3",
        day,
        model=CHALLENGER,
        alias="challenger",
        live=live_rows(df, day),
    )
    challenger_path = forecast_path(cfg, "SE3", day, "challenger")
    assert res.role == "challenger"
    assert res.path == challenger_path and challenger_path.exists()
    assert not forecast_path(cfg, "SE3", day).exists()
    assert state_dir(cfg, "SE3", CHALLENGER).exists()
    written = pd.read_parquet(challenger_path)
    assert set(written["model"]) == {CHALLENGER}
    run = mlflow.get_run(res.run_id)
    assert run.data.tags["role"] == "challenger"
