from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from pricefc.config import load_ingest_config
from pricefc.datasets import features as F
from pricefc.datasets.build import DatasetBuilder, LeakageError, target_hours
from pricefc.datasets.live import LiveDataError, build_live_rows, live_dir
from pricefc.datasets.sources import TimedSource
from tests.test_dataset import base_in
from tests.test_leakage import builder as synthetic_builder

INGEST = load_ingest_config(Path("configs/ingest.yaml"))
DAY = date(2025, 4, 5)


def _patch_inputs(
    monkeypatch: pytest.MonkeyPatch,
    builder: DatasetBuilder,
    reference: pd.DataFrame,
) -> None:
    monkeypatch.setattr(
        DatasetBuilder,
        "from_snapshots",
        classmethod(lambda cls, *args, **kwargs: builder),
    )
    ref = SimpleNamespace(version="synthetic-20261006", read=lambda: reference)
    monkeypatch.setattr("pricefc.backtest.run.latest_dataset", lambda *args: ref)


def _live(
    monkeypatch: pytest.MonkeyPatch,
    builder: DatasetBuilder,
    reference: pd.DataFrame,
):
    _patch_inputs(monkeypatch, builder, reference)
    return build_live_rows(builder.base, builder.features, INGEST, "SE3", DAY, write=False)


def _drop_target_prices(builder: DatasetBuilder) -> None:
    targets = target_hours(DAY + timedelta(days=1), builder.base.timezone)
    source = builder.sources[F.PRICE]
    builder.sources[F.PRICE] = TimedSource(
        source.name, source.data.drop(index=targets, errors="ignore"), source.rule
    )


def test_live_rows_match_normal_origin_without_target_prices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = synthetic_builder()
    builder.base = base_in(tmp_path)
    normal = builder.build_origin(DAY)
    _drop_target_prices(builder)

    live = _live(monkeypatch, builder, normal)

    features = builder.feature_columns(normal)
    pd.testing.assert_frame_equal(live.frame[features], normal[features], check_exact=True)
    assert live.frame[["y", "y_n_periods", "y_is_pt15m"]].isna().all().all()
    assert live.report["leakage_audit"]["passed"]
    assert live.report["leakage_audit"]["target_change_required"] is False
    assert live.path is None


def test_live_rows_reject_missing_weather_for_target_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = synthetic_builder()
    builder.base = base_in(tmp_path)
    reference = builder.build_origin(DAY)
    source = builder.sources["wx_a"]
    missing_at = target_hours(DAY + timedelta(days=1), builder.base.timezone)[3]
    builder.sources["wx_a"] = TimedSource(
        source.name, source.data.drop(index=missing_at), source.rule
    )
    _patch_inputs(monkeypatch, builder, reference)

    with pytest.raises(LiveDataError, match=r"wx_a.*missing.*2025"):
        build_live_rows(builder.base, builder.features, INGEST, "SE3", DAY, write=False)


def test_live_rows_reject_new_feature_nan_against_latest_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = synthetic_builder()
    builder.base = base_in(tmp_path)
    reference = builder.build_origin(DAY)
    source = builder.sources["wx_a"]
    missing_at = target_hours(DAY + timedelta(days=1), builder.base.timezone)[3]
    builder.sources["wx_a"] = TimedSource(
        source.name, source.data.drop(index=missing_at), source.rule
    )
    monkeypatch.setattr(builder, "live_origin_problems", lambda day: [])
    _patch_inputs(monkeypatch, builder, reference)

    with pytest.raises(LiveDataError, match=r"new NaN.*w_a_temperature_2m"):
        build_live_rows(builder.base, builder.features, INGEST, "SE3", DAY, write=False)


def test_live_leakage_audit_catches_a_post_origin_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = synthetic_builder()
    builder.base = base_in(tmp_path)
    reference = builder.build_origin(DAY)

    def leaky(ctx: F.OriginContext) -> pd.DataFrame:
        values = ctx.sources[F.PRICE].data["price"].reindex(ctx.targets).to_numpy()
        return pd.DataFrame({"leaky_tomorrow_price": values}, index=ctx.targets)

    builder.groups.append(F.FeatureGroup("injected_leak", (F.PRICE,), leaky))
    _patch_inputs(monkeypatch, builder, reference)

    with pytest.raises(LeakageError) as err:
        build_live_rows(builder.base, builder.features, INGEST, "SE3", DAY, write=False)
    assert err.value.report["leaky_features"] == ["leaky_tomorrow_price"]
    assert err.value.report["target_change_required"] is False
    assert err.value.report["post_origin_source_values_changed"] > 0


def test_live_rows_reject_column_contract_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = synthetic_builder()
    builder.base = base_in(tmp_path)
    reference = builder.build_origin(DAY).drop(columns="p_lag1d")
    _patch_inputs(monkeypatch, builder, reference)

    with pytest.raises(LiveDataError, match=r"columns.*rebuild datasets first"):
        build_live_rows(builder.base, builder.features, INGEST, "SE3", DAY, write=False)


def test_live_rows_reject_dtype_contract_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = synthetic_builder()
    builder.base = base_in(tmp_path)
    reference = builder.build_origin(DAY).astype({"cal_hour": "float64"})
    _patch_inputs(monkeypatch, builder, reference)

    with pytest.raises(LiveDataError, match=r"dtypes.*cal_hour.*rebuild datasets first"):
        build_live_rows(builder.base, builder.features, INGEST, "SE3", DAY, write=False)


def test_live_rows_write_and_replace_manifest_and_parquet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = synthetic_builder()
    builder.base = base_in(tmp_path)
    normal = builder.build_origin(DAY)
    _drop_target_prices(builder)
    _patch_inputs(monkeypatch, builder, normal)
    monkeypatch.setenv("PRICEFC_GIT_SHA", "abc123")

    first = build_live_rows(builder.base, builder.features, INGEST, "SE3", DAY)
    assert first.path == live_dir(builder.base, "SE3", DAY)
    assert first.path is not None
    assert (first.path / "rows.parquet").is_file()
    assert (first.path / "manifest.json").is_file()
    pd.testing.assert_frame_equal(pd.read_parquet(first.path / "rows.parquet"), first.frame)
    manifest = json.loads((first.path / "manifest.json").read_text())
    assert manifest["zone"] == "SE3"
    assert manifest["origin_date"] == DAY.isoformat()
    assert manifest["git_sha"] == "abc123"
    assert manifest["eval_dataset_version"] == "synthetic-20261006"
    assert manifest["audit_report"]["passed"]
    assert manifest["source_lineage"] == first.report["source_lineage"]

    monkeypatch.setenv("PRICEFC_GIT_SHA", "def456")
    second = build_live_rows(builder.base, builder.features, INGEST, "SE3", DAY)
    assert second.path == first.path
    replaced = json.loads((second.path / "manifest.json").read_text())
    assert replaced["git_sha"] == "def456"
