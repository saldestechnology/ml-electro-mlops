from datetime import UTC, date, datetime
from pathlib import Path

import mlflow
import numpy as np
import pandas as pd
import pytest

from pricefc.config import Location, load_config, load_features_config
from pricefc.datasets import features as F
from pricefc.datasets.build import META_COLUMNS, target_hours
from pricefc.datasets.loaders import load_hourly_prices, load_weather
from pricefc.datasets.registry import find_datasets, log_dataset_run, write_dataset
from pricefc.ingest.snapshot import write_snapshot
from tests.test_leakage import builder

TZ = "Europe/Stockholm"
FEATS = load_features_config(Path("configs/features.yaml"))


def base_in(tmp_path: Path):  # type: ignore[no-untyped-def]
    return load_config(
        Path("configs/base.yaml"),
        {
            "paths": {
                "data_root": str(tmp_path),
                "raw": str(tmp_path / "raw"),
                "datasets": str(tmp_path / "datasets"),
            },
            "mlflow": {
                "tracking_uri": f"sqlite:///{tmp_path}/m.db",
                "artifact_root": str(tmp_path / "art"),
            },
        },
    )


# --- calendar ---------------------------------------------------------------------------------


def calendar_for(target_day: date) -> pd.DataFrame:
    origin_day = target_day - pd.Timedelta(days=1)
    ctx = F.OriginContext(
        "SE3",
        origin_day,
        pd.Timestamp("2000-01-01", tz="UTC"),
        target_hours(target_day, TZ),
        TZ,
        {},
    )
    return F.calendar_features(ctx)


@pytest.mark.parametrize(
    ("day", "flag"),
    [
        (date(2025, 6, 20), "cal_is_de_facto_holiday"),  # Midsummer Eve
        (date(2025, 12, 24), "cal_is_de_facto_holiday"),  # Christmas Eve
        (date(2025, 6, 6), "cal_is_public_holiday"),  # National Day
        (date(2025, 5, 30), "cal_is_bridge_day"),  # Friday after Ascension
        (date(2025, 4, 30), "cal_is_half_day"),  # Walpurgis Night
    ],
)
def test_swedish_calendar_flags(day: date, flag: str) -> None:
    assert calendar_for(day)[flag].iloc[0] == 1


def test_plain_sunday_is_weekend_not_holiday() -> None:
    cal = calendar_for(date(2025, 9, 14))
    assert cal["cal_is_weekend"].iloc[0] == 1
    assert cal["cal_is_public_holiday"].iloc[0] == 0


def test_dst_days_have_right_hours() -> None:
    spring, autumn = calendar_for(date(2025, 3, 30)), calendar_for(date(2025, 10, 26))
    assert len(spring) == 23 and 2 not in spring["cal_hour"].tolist()
    assert len(autumn) == 25 and autumn["cal_hour"].tolist().count(2) == 2
    assert autumn["cal_is_dst"].tolist()[2:4] == [1, 0]


# --- same local hour across DST -----------------------------------------------------------------


def test_same_local_hour_lag_across_dst() -> None:
    # Target day after spring-forward: its 02:00 did not exist the day before -> NaT.
    after_spring = F.same_local_hour(target_hours(date(2025, 3, 31), TZ), TZ, 1)
    local = pd.DatetimeIndex(after_spring.dropna()).tz_convert(TZ)
    assert after_spring.isna().sum() == 1 and 2 not in local.hour
    # 25-hour day: both 02:00 hours map to the single 02:00 of the previous day.
    autumn = F.same_local_hour(target_hours(date(2025, 10, 26), TZ), TZ, 1)
    assert autumn[2] == autumn[3] == pd.Timestamp("2025-10-25 02:00", tz=TZ)


# --- loaders ----------------------------------------------------------------------------------


def snapshot(tmp_path: Path, source: str, dataset: str, key: str, df: pd.DataFrame) -> None:
    write_snapshot(
        df,
        raw_root=tmp_path,
        source=source,
        dataset=dataset,
        key=key,
        endpoint="",
        query={},
        requested_start=df["timestamp"].min(),
        requested_end=df["timestamp"].max(),
        pulled_at=datetime(2026, 1, 1, tzinfo=UTC),
        validation={"passed": True},
    )


def test_price_loader_averages_quarter_hours(tmp_path: Path) -> None:
    ts = pd.date_range("2025-10-01 00:00", periods=8, freq="15min", tz="UTC")
    df = pd.DataFrame(
        {"timestamp": ts, "price_eur_mwh": [1.0, 2, 3, 4, 10, 10, 10, 30], "resolution": "PT15M"}
    )
    snapshot(tmp_path, "elprisetjustnu", "day_ahead_prices", "SE3", df)
    base = base_in(tmp_path)
    src, snaps = load_hourly_prices(tmp_path, "elprisetjustnu", "SE3", base, FEATS.dataset, "p")
    assert src.data["price"].tolist() == [2.5, 15.0]
    assert src.data["is_pt15m"].tolist() == [1.0, 1.0] and len(snaps) == 1


def test_weather_loader_strips_suffix_and_realigns_preceding_hour_vars(tmp_path: Path) -> None:
    variables = ["temperature_2m", "shortwave_radiation"]
    ts = pd.date_range("2025-06-01", periods=4, freq="h", tz="UTC")
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "temperature_2m_previous_day2": [10.0, 11, 12, 13],
            "shortwave_radiation_previous_day2": [0.0, 100, 200, 300],
        }
    )
    snapshot(tmp_path, "open_meteo", "previous_runs__ecmwf_ifs", "loc", df)
    loc = Location(name="loc", lat=0, lon=0, role="demand")
    src, _ = load_weather(tmp_path, loc, "true_lead", FEATS.dataset, variables)
    row = src.data.loc[pd.Timestamp("2025-06-01 01:00", tz="UTC")]
    # Radiation reported at 02:00 covers 01:00-02:00, so it belongs to the 01:00 hour.
    assert row["temperature_2m"] == 11 and row["shortwave_radiation"] == 200
    assert src.rule.kind == "fixed_lead" and src.rule.params["lead_hours"] == 48


# --- versioned storage and MLflow ---------------------------------------------------------------


def test_build_write_and_log(tmp_path: Path) -> None:
    base = base_in(tmp_path)
    b = builder()
    b.base = base
    df, info = b.build(date(2025, 3, 25), date(2025, 4, 2))
    assert info["rows_built"] == 9 * 24 - 1  # one 23-hour target day (2025-03-30)
    assert not df[b.feature_columns(df)].columns.str.startswith("y").any()
    report = b.leakage_audit(b.audit_origins(date(2025, 3, 25), date(2025, 4, 2)))
    info["meta_columns"] = list(META_COLUMNS)
    desc = {"weather_kind": "true_lead", "weather_source": "synthetic", "sources": {}}
    kwargs = {
        "base": base,
        "features": FEATS,
        "zone": "SE3",
        "name": "se3-test",
        "description": desc,
        "build_info": info,
        "leakage_report": report,
        "built": datetime(2026, 10, 1, tzinfo=UTC),
    }
    ds = write_dataset(df, **kwargs)  # type: ignore[arg-type]
    assert ds.path == tmp_path / "datasets" / "SE3" / "hourly" / ds.version
    assert ds.version.startswith("20261001-") and ds.manifest["leakage_audit"]["passed"]
    again = write_dataset(df, **kwargs)  # type: ignore[arg-type]
    assert again.path == ds.path  # same content, same version
    pd.testing.assert_frame_equal(ds.read(), df)
    assert [d.version for d in find_datasets(base, "SE3")] == [ds.version]

    run_id = log_dataset_run(base, ds, df)
    run = mlflow.get_run(run_id)
    assert run.data.tags["dataset_version"] == ds.version
    assert run.inputs.dataset_inputs[0].dataset.name == "se3-test"
    assert np.isclose(float(run.data.params["rows"]), len(df))
