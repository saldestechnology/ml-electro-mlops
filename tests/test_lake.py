from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from pricefc.config import load_config, load_features_config
from pricefc.datasets.loaders import load_hourly_prices, load_weather
from pricefc.ingest.snapshot import load_latest, write_snapshot
from pricefc.lake import build_silver
from pricefc.lake.query import connect, execute_readonly, table_metadata

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
T2 = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _base(tmp_path: Path):  # type: ignore[no-untyped-def]
    return load_config(
        Path("configs/base.yaml"),
        {
            "paths": {
                "data_root": str(tmp_path),
                "raw": str(tmp_path / "raw"),
                "datasets": str(tmp_path / "datasets"),
            }
        },
    )


def _write(
    raw_root: Path,
    df: pd.DataFrame,
    *,
    source: str,
    dataset: str,
    key: str,
    pulled_at: datetime,
    passed: bool = True,
    extra: dict[str, Any] | None = None,
) -> None:
    write_snapshot(
        df,
        raw_root=raw_root,
        source=source,
        dataset=dataset,
        key=key,
        endpoint="synthetic",
        query={},
        requested_start=pd.Timestamp(df["timestamp"].min()),
        requested_end=pd.Timestamp(df["timestamp"].max()) + pd.Timedelta(hours=1),
        pulled_at=pulled_at,
        validation={"passed": passed},
        extra=extra,
    )


def _read_table(path: Path) -> pd.DataFrame:
    return (
        pd.concat(
            [pd.read_parquet(file) for file in sorted(path.glob("year=*/part-0.parquet"))],
            ignore_index=True,
        )
        .sort_values("timestamp", kind="stable")
        .reset_index(drop=True)
    )


def _price_frame(hours: list[str], prices: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(hours, utc=True),
            "price_eur_mwh": prices,
            "resolution": "PT60M",
        }
    )


def test_silver_matches_latest_and_dataset_loaders(tmp_path: Path) -> None:
    base = _base(tmp_path)
    price_initial = _price_frame(
        ["2026-01-01 00:00", "2026-01-01 01:00", "2026-01-01 02:00"],
        [10.0, 20.0, 30.0],
    )
    price_revision = _price_frame(
        ["2026-01-01 01:00", "2026-01-01 02:00", "2026-01-01 03:00"],
        [22.0, 30.0, 40.0],
    )
    price_invalid = _price_frame(["2026-01-01 01:00"], [999.0])
    _write(
        base.paths.raw,
        price_initial,
        source="elprisetjustnu",
        dataset="day_ahead_prices",
        key="SE3",
        pulled_at=T0,
    )
    _write(
        base.paths.raw,
        price_revision,
        source="elprisetjustnu",
        dataset="day_ahead_prices",
        key="SE3",
        pulled_at=T1,
    )
    _write(
        base.paths.raw,
        price_invalid,
        source="elprisetjustnu",
        dataset="day_ahead_prices",
        key="SE3",
        pulled_at=T2,
        passed=False,
    )

    weather_col = "temperature_2m_previous_day2"
    weather_initial = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC"),
            weather_col: [1.0, 2.0, 3.0],
        }
    )
    weather_revision = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01 01:00", periods=3, freq="h", tz="UTC"),
            weather_col: [2.5, 3.0, 4.0],
        }
    )
    _write(
        base.paths.raw,
        weather_initial,
        source="open_meteo",
        dataset="previous_runs__ecmwf_ifs",
        key="se3_gavle",
        pulled_at=T0,
    )
    _write(
        base.paths.raw,
        weather_revision,
        source="open_meteo",
        dataset="previous_runs__ecmwf_ifs",
        key="se3_gavle",
        pulled_at=T1,
    )

    report = build_silver(base)
    assert set(report["built"]) == {
        "prices_se3",
        "weather_previous_runs_se3_gavle",
    }
    assert report["conflicts"] == 2

    silver_root = tmp_path / "lake" / "silver"
    silver_prices = _read_table(silver_root / "elprisetjustnu" / "day_ahead_prices" / "SE3")
    latest_prices, _ = load_latest(base.paths.raw, "elprisetjustnu", "day_ahead_prices", "SE3")
    pd.testing.assert_frame_equal(
        silver_prices[latest_prices.columns],
        latest_prices.sort_values("timestamp", kind="stable").reset_index(drop=True),
        check_dtype=False,
    )
    features = load_features_config(Path("configs/features.yaml"))
    price_source, _ = load_hourly_prices(
        base.paths.raw, "elprisetjustnu", "SE3", base, features.dataset, "price"
    )
    silver_hourly = silver_prices.assign(hour=silver_prices["timestamp"].dt.floor("h"))
    expected_hourly = silver_hourly.groupby("hour")["price_eur_mwh"].mean().sort_index()
    expected_hourly.index = expected_hourly.index.astype("datetime64[ns, UTC]")
    loader_hourly = price_source.data["price"].rename_axis("hour")
    loader_hourly.index = loader_hourly.index.astype("datetime64[ns, UTC]")
    pd.testing.assert_series_equal(
        expected_hourly,
        loader_hourly,
        check_names=False,
    )

    silver_weather = _read_table(
        silver_root / "open_meteo" / "previous_runs__ecmwf_ifs" / "se3_gavle"
    )
    latest_weather, _ = load_latest(
        base.paths.raw, "open_meteo", "previous_runs__ecmwf_ifs", "se3_gavle"
    )
    pd.testing.assert_frame_equal(
        silver_weather[latest_weather.columns],
        latest_weather.sort_values("timestamp", kind="stable").reset_index(drop=True),
        check_dtype=False,
    )
    location = next(
        loc
        for locations in features.weather.locations.values()
        for loc in locations
        if loc.name == "se3_gavle"
    )
    weather_source, _ = load_weather(
        base.paths.raw, location, "true_lead", features.dataset, ["temperature_2m"]
    )
    silver_temperature = silver_weather.set_index("timestamp")[weather_col].rename("temperature_2m")
    silver_temperature.index = silver_temperature.index.astype("datetime64[ns, UTC]")
    loader_temperature = weather_source.data["temperature_2m"].rename_axis("timestamp")
    loader_temperature.index = loader_temperature.index.astype("datetime64[ns, UTC]")
    pd.testing.assert_series_equal(
        silver_temperature,
        loader_temperature,
        check_names=True,
    )

    conflicts = pd.read_parquet(
        silver_root / "elprisetjustnu" / "day_ahead_prices" / "SE3" / "_conflicts.parquet"
    )
    assert len(conflicts) == 1
    assert conflicts.loc[0, "column"] == "price_eur_mwh"
    assert conflicts.loc[0, "old"] == "20.0" and conflicts.loc[0, "new"] == "22.0"
    assert pd.Timestamp(conflicts.loc[0, "old_pulled_at"]) == pd.Timestamp(T0)
    assert pd.Timestamp(conflicts.loc[0, "new_pulled_at"]) == pd.Timestamp(T1)

    second = build_silver(base)
    assert second["built"] == []
    assert set(second["skipped"]) == {
        "prices_se3",
        "weather_previous_runs_se3_gavle",
    }


def test_forecast_vintages_are_append_style_even_for_identical_values(tmp_path: Path) -> None:
    base = _base(tmp_path)
    forecast = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-09-01", periods=2, freq="h", tz="UTC"),
            "temperature_2m": [12.0, 13.0],
        }
    )
    _write(
        base.paths.raw,
        forecast,
        source="open_meteo",
        dataset="forecast__ecmwf_ifs",
        key="se3_gavle",
        pulled_at=T0,
    )
    assert build_silver(base)["rows"] == 2
    _write(
        base.paths.raw,
        forecast,
        source="open_meteo",
        dataset="forecast__ecmwf_ifs",
        key="se3_gavle",
        pulled_at=T1,
    )
    report = build_silver(base)
    assert report["built"] == ["weather_forecast_se3_gavle"]
    assert report["rows"] == 4 and report["conflicts"] == 0
    silver = _read_table(
        tmp_path / "lake" / "silver" / "open_meteo" / "forecast__ecmwf_ifs" / "se3_gavle"
    )
    assert silver.groupby("timestamp").size().eq(2).all()
    assert silver["_pulled_at"].nunique() == 2
    metadata = table_metadata(base)[0]
    assert metadata["collapse"] == "append_by_pulled_at"
    assert build_silver(base)["skipped"] == ["weather_forecast_se3_gavle"]


def test_known_bad_price_days_are_removed_and_lineage_is_kept(tmp_path: Path) -> None:
    base = _base(tmp_path)
    prices = _price_frame(["2026-01-01 00:00", "2026-01-02 00:00"], [999.0, 15.0])
    _write(
        base.paths.raw,
        prices,
        source="elprisetjustnu",
        dataset="day_ahead_prices",
        key="SE3",
        pulled_at=T0,
        extra={"chunks": [{"excluded_known_bad_days": {"2026-01-01": 24}}]},
    )
    build_silver(base)
    silver = _read_table(
        tmp_path / "lake" / "silver" / "elprisetjustnu" / "day_ahead_prices" / "SE3"
    )
    assert silver["timestamp"].dt.date.astype(str).tolist() == ["2026-01-02"]
    assert {"_pulled_at", "_snapshot"} <= set(silver.columns)
    assert silver.loc[0, "_pulled_at"] == pd.Timestamp(T0)
    assert silver.loc[0, "_snapshot"].startswith("elprisetjustnu/day_ahead_prices/SE3/")


def test_duckdb_views_and_read_only_sql(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    base = _base(tmp_path)
    prices = _price_frame(["2026-01-01 00:00"], [12.5])
    _write(
        base.paths.raw,
        prices,
        source="elprisetjustnu",
        dataset="day_ahead_prices",
        key="SE3",
        pulled_at=T0,
    )
    build_silver(base)
    connection = connect(base)
    result = execute_readonly(connection, "SELECT zone, price_eur_mwh FROM prices")
    assert result.fetchall() == [("SE3", 12.5)]
    with pytest.raises(ValueError, match="read-only SELECT"):
        execute_readonly(connection, "DROP VIEW prices")
    assert connection.execute("SELECT count(*) FROM prices").fetchone() == (1,)


def test_empty_valid_snapshot_has_an_empty_query_view(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    base = _base(tmp_path)
    empty = pd.DataFrame(
        {
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            "value": pd.Series(dtype="float64"),
        }
    )
    _write(
        base.paths.raw,
        empty,
        source="synthetic",
        dataset="empty_series",
        key="k1",
        pulled_at=T0,
    )
    build_silver(base)
    connection = connect(base)
    assert connection.execute("SELECT count(*) FROM synthetic_empty_series_k1").fetchone() == (0,)
