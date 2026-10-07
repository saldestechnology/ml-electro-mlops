from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from pricefc.config import BaseConfig, load_config, load_features_config, load_ingest_config
from pricefc.datasets.loaders import load_hourly_prices
from pricefc.ingest.snapshot import write_snapshot
from pricefc.serving import score as score_module
from pricefc.serving.score import DailyScore, load_actuals, score_forecasts
from pricefc.timeutils import local_day_bounds_utc

TZ = ZoneInfo("Europe/Stockholm")


@pytest.fixture
def cfg(tmp_path: Path) -> BaseConfig:
    root = tmp_path / "pricefc-data"
    return load_config(
        Path("configs/base.yaml"),
        {"paths": {"data_root": str(root), "raw": str(root / "raw"), "datasets": str(root)}},
    )


def _set_now(monkeypatch: pytest.MonkeyPatch, today: date) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: ZoneInfo | None = None) -> FrozenDateTime:
            return cls.combine(today, datetime.min.time(), tzinfo=tz or TZ).replace(hour=20)

    monkeypatch.setattr(score_module, "datetime", FrozenDateTime)


def _target_times(day: date, timezone: str = "Europe/Stockholm") -> pd.DatetimeIndex:
    start, end = local_day_bounds_utc(day, timezone)
    return pd.date_range(start, end, freq="h", inclusive="left", name="target_time")


def _write_snapshot(cfg: BaseConfig, frame: pd.DataFrame) -> None:
    times = pd.DatetimeIndex(frame["timestamp"])
    write_snapshot(
        frame,
        raw_root=cfg.paths.raw,
        source="elprisetjustnu",
        dataset="day_ahead_prices",
        key="SE3",
        endpoint="test",
        query={},
        requested_start=pd.Timestamp(times.min()),
        requested_end=pd.Timestamp(times.max()),
        pulled_at=datetime(2026, 10, 1, tzinfo=UTC),
        validation={"passed": True},
    )


def _write_prices(
    cfg: BaseConfig, days: list[date], missing: set[pd.Timestamp] | None = None
) -> None:
    missing = missing or set()
    times = pd.DatetimeIndex(
        [stamp for day in days for stamp in _target_times(day) if stamp not in missing]
    )
    frame = pd.DataFrame(
        {
            "timestamp": times,
            "price_eur_mwh": [10.0] * len(times),
            "resolution": "PT60M",
        }
    )
    _write_snapshot(cfg, frame)


def _write_forecast(
    cfg: BaseConfig,
    origin: date,
    role: str = "champion",
    target_times: pd.DatetimeIndex | None = None,
) -> Path:
    target_times = (
        target_times if target_times is not None else _target_times(origin + timedelta(days=1))
    )
    root = cfg.paths.data_root / "forecasts" / "SE3"
    if role == "challenger":
        root /= "challenger"
    root.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {
            "origin_date": origin.isoformat(),
            "origin": pd.Timestamp(datetime.combine(origin, datetime.min.time(), tzinfo=TZ)),
            "target_time": target_times,
            "target_date": (origin + timedelta(days=1)).isoformat(),
            "y": float("nan"),
            "model": "ensemble_hourly_exp",
            "model_version": "17",
            "forecast_made_at": pd.Timestamp("2026-10-01T08:00:00Z"),
            **{
                f"q{round(q * 100):02d}": [10.0 + offset] * len(target_times)
                for q, offset in zip(
                    cfg.quantiles, (-5.0, -3.0, -1.0, 0.0, 1.0, 3.0, 5.0), strict=True
                )
            },
        }
    )
    path = root / f"{origin.isoformat()}.parquet"
    frame.to_parquet(path, index=False)
    return path


def test_load_actuals_uses_dataset_hourly_price_aggregation(
    cfg: BaseConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = pd.Timestamp("2026-10-02T00:00:00Z")
    frame = pd.DataFrame(
        {
            "timestamp": [start + pd.Timedelta(minutes=15 * i) for i in range(4)]
            + [start + pd.Timedelta(hours=1)],
            "price_eur_mwh": [1.0, 2.0, 3.0, 4.0, 10.0],
            "resolution": ["PT15M"] * 4 + ["PT60M"],
        }
    )
    _write_snapshot(cfg, frame)
    features = load_features_config(Path("configs/features.yaml"))
    ingest = load_ingest_config(Path("configs/ingest.yaml"))
    hourly, _ = load_hourly_prices(
        cfg.paths.raw, ingest.prices.source, "SE3", cfg, features.dataset, "price"
    )

    loader = score_module.load_hourly_prices
    reads = 0

    def count_reads(*args: Any, **kwargs: Any) -> Any:
        nonlocal reads
        reads += 1
        return loader(*args, **kwargs)

    monkeypatch.setattr(score_module, "load_hourly_prices", count_reads)
    actuals = load_actuals(cfg, "SE3")
    cached = load_actuals(cfg, "SE3")
    assert actuals is not None
    assert cached is actuals and reads == 1
    for stamp, value in hourly.data["price"].items():
        assert actuals.by_utc_ns[pd.Timestamp(stamp).value] == value
    assert actuals.by_utc_ns[start.value] == 2.5


def test_score_forecasts_scores_a_fully_published_day(
    cfg: BaseConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = date(2026, 10, 8)
    target = origin + timedelta(days=1)
    _set_now(monkeypatch, date(2026, 10, 9))
    _write_prices(cfg, [target])
    _write_forecast(cfg, origin)

    scores = score_forecasts(cfg, zones=["SE3"], log_to_mlflow=False)

    assert len(scores) == 1
    item = scores[0]
    assert item.zone == "SE3" and item.role == "champion"
    assert item.origin_date == origin and item.target_date == target
    assert item.n_hours == 24 and item.model_version == "17"
    assert item.coverage_90 == 1.0
    assert item.pinball >= 0
    stored = pd.read_parquet(cfg.paths.data_root / "scores" / "SE3" / "champion.parquet")
    assert stored["origin_date"].astype(str).tolist() == [origin.isoformat()]


def test_score_forecasts_skips_day_with_missing_actual_hour(
    cfg: BaseConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = date(2026, 10, 8)
    target_times = _target_times(origin + timedelta(days=1))
    _set_now(monkeypatch, date(2026, 10, 9))
    _write_prices(cfg, [origin + timedelta(days=1)], {target_times[5]})
    _write_forecast(cfg, origin)

    assert score_forecasts(cfg, zones=["SE3"], log_to_mlflow=False) == []
    assert not (cfg.paths.data_root / "scores" / "SE3" / "champion.parquet").exists()


def test_score_forecasts_skips_existing_origins_and_force_upserts(
    cfg: BaseConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = date(2026, 10, 8)
    _set_now(monkeypatch, date(2026, 10, 9))
    _write_prices(cfg, [origin + timedelta(days=1)])
    _write_forecast(cfg, origin)
    store = cfg.paths.data_root / "scores" / "SE3" / "champion.parquet"
    store.parent.mkdir(parents=True)
    old = DailyScore(
        "SE3",
        "champion",
        origin,
        origin + timedelta(days=1),
        datetime(2026, 10, 8, tzinfo=UTC),
        24,
        "old",
        999.0,
        None,
        None,
        0.0,
        0.0,
        999.0,
    )
    pd.DataFrame([old.__dict__, old.__dict__]).to_parquet(store, index=False)

    assert score_forecasts(cfg, zones=["SE3"], log_to_mlflow=False) == []
    assert len(score_forecasts(cfg, zones=["SE3"], force=True, log_to_mlflow=False)) == 1
    stored = pd.read_parquet(store)
    assert len(stored) == 1
    assert stored.loc[0, "origin_date"] == origin
    assert stored.loc[0, "pinball"] != 999.0


def test_score_forecasts_keeps_champion_and_challenger_separate(
    cfg: BaseConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = date(2026, 10, 8)
    _set_now(monkeypatch, date(2026, 10, 9))
    _write_prices(cfg, [origin + timedelta(days=1)])
    _write_forecast(cfg, origin, "champion")
    _write_forecast(cfg, origin, "challenger")

    scores = score_forecasts(cfg, zones=["SE3"], log_to_mlflow=False)

    assert {item.role for item in scores} == {"champion", "challenger"}
    for role in ("champion", "challenger"):
        store = pd.read_parquet(cfg.paths.data_root / "scores" / "SE3" / f"{role}.parquet")
        assert len(store) == 1 and store.loc[0, "role"] == role


def test_dst_delivery_day_scores_all_25_hours(
    cfg: BaseConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = date(2026, 10, 24)
    target = date(2026, 10, 25)
    times = _target_times(target)
    assert len(times) == 25
    _set_now(monkeypatch, date(2026, 10, 26))
    _write_prices(cfg, [target])
    _write_forecast(cfg, origin, target_times=times)

    scores = score_forecasts(cfg, zones=["SE3"], log_to_mlflow=False)

    assert len(scores) == 1 and scores[0].n_hours == 25


def test_mlflow_failure_does_not_remove_persisted_score(
    cfg: BaseConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = date(2026, 10, 8)
    _set_now(monkeypatch, date(2026, 10, 9))
    _write_prices(cfg, [origin + timedelta(days=1)])
    _write_forecast(cfg, origin)

    def fail(_base: BaseConfig, _item: DailyScore) -> None:
        raise OSError("tracking server offline")

    result = score_forecasts(cfg, zones=["SE3"], mlflow_logger=fail)

    assert len(result) == 1
    assert (cfg.paths.data_root / "scores" / "SE3" / "champion.parquet").is_file()
