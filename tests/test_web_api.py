"""The web API reads forecasts and metadata without loading served-state pickles."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from pricefc.config import BaseConfig, load_config
from pricefc.ingest.snapshot import write_snapshot
from pricefc.web import metrics as web_metrics
from pricefc.web.app import create_app

TZ = ZoneInfo("Europe/Stockholm")
QUANTILES = [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95]
QS = ["q05", "q10", "q25", "q50", "q75", "q90", "q95"]
HAND_FORECAST = [0.0, 2.0, 4.0, 8.0, 12.0, 16.0, 20.0]
PROM_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+([^\s]+)$")
PROM_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"(?:,|$)')


@pytest.fixture
def cfg(tmp_path: Path) -> BaseConfig:
    root = tmp_path / "data"
    return load_config(
        Path("configs/base.yaml"),
        {"paths": {"data_root": str(root), "raw": str(root / "raw"), "datasets": str(root)}},
    )


def _local_time(day: date, hour: int) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(day, time(hour), tzinfo=TZ))


def _forecast_frame(
    origin: date,
    target_times: pd.DatetimeIndex,
    quantile_values: list[float] = HAND_FORECAST,
    model: str = "ensemble_hourly_exp",
) -> pd.DataFrame:
    target_local = target_times.tz_convert(TZ)
    frame: dict[str, Any] = {
        "origin_date": [origin.isoformat()] * len(target_times),
        "origin": [pd.Timestamp(datetime.combine(origin, time(9), tzinfo=TZ))] * len(target_times),
        "target_time": target_times,
        "target_date": [stamp.date().isoformat() for stamp in target_local],
        "y": [float("nan")] * len(target_times),
        "model": [model] * len(target_times),
        "model_version": ["17"] * len(target_times),
        "forecast_made_at": [pd.Timestamp("2026-10-06T08:00:00Z")] * len(target_times),
    }
    frame.update(
        {
            column: [value] * len(target_times)
            for column, value in zip(QS, quantile_values, strict=True)
        }
    )
    return pd.DataFrame(frame)


def _write_forecast(
    cfg: BaseConfig,
    zone: str,
    origin: date,
    target_times: pd.DatetimeIndex,
    quantile_values: list[float] = HAND_FORECAST,
    role: str = "champion",
    model: str = "ensemble_hourly_exp",
) -> Path:
    root = cfg.paths.data_root / "forecasts" / zone
    if role == "challenger":
        root /= role
    path = root / f"{origin.isoformat()}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    _forecast_frame(origin, target_times, quantile_values, model).to_parquet(path, index=False)
    return path


def _actual_frame(forecast_time: pd.Timestamp, y: float, naive_y: float) -> pd.DataFrame:
    forecast_local = forecast_time.tz_convert(TZ)
    naive_local = forecast_local - pd.DateOffset(days=7)
    return pd.DataFrame(
        {
            "target_time": [forecast_time, naive_local.tz_convert("UTC")],
            "y": [y, naive_y],
        }
    )


def _write_actuals(cfg: BaseConfig, frame: pd.DataFrame) -> None:
    raw = frame.rename(columns={"target_time": "timestamp", "y": "price_eur_mwh"})
    raw["resolution"] = "PT60M"
    times = pd.DatetimeIndex(raw["timestamp"])
    write_snapshot(
        raw,
        raw_root=cfg.paths.raw,
        source="elprisetjustnu",
        dataset="day_ahead_prices",
        key="SE3",
        endpoint="test",
        query={},
        requested_start=times.min(),
        requested_end=times.max(),
        pulled_at=datetime(2026, 10, 4, 11, tzinfo=UTC),
        validation={"passed": True},
    )


def test_endpoints_metrics_cache_and_readable_state(
    cfg: BaseConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRICEFC_ENV", "dev")
    monkeypatch.setenv("PRICEFC_GIT_SHA", "abc123")
    today = datetime.now(TZ).date()
    target_time = _local_time(today + timedelta(days=1), 12).tz_convert("UTC")
    old_origin = today - timedelta(days=1)
    _write_forecast(cfg, "SE3", old_origin, pd.DatetimeIndex([target_time - pd.Timedelta(days=1)]))
    _write_forecast(cfg, "SE3", today, pd.DatetimeIndex([target_time]))
    _write_forecast(
        cfg,
        "SE3",
        today,
        pd.DatetimeIndex([target_time]),
        [1.0, 3.0, 5.0, 9.0, 13.0, 17.0, 21.0],
        role="challenger",
        model="ensemble_hourly_exp_tfm3",
    )
    _write_forecast(cfg, "SE2", today - timedelta(days=2), pd.DatetimeIndex([target_time]))

    state_dir = cfg.paths.data_root / "state" / "SE3" / "ensemble_hourly_exp"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        '{"format_version":1,"zone":"SE3","spec":"ensemble_hourly_exp",'
        '"source_version":"17","last_fit":"2026-10-01","first_origin":"2026-01-01",'
        '"last_origin":"2026-10-06","origins_served":9,"train_start":"2021-01-01",'
        '"datasets":{"train":"train-v1","eval":"eval-v1","live":"live-v1"},'
        '"versions":{"python":"3.12"},"git_sha":"saved-sha",'
        '"saved_at":"2026-10-06T08:05:00+00:00"}'
    )
    # Invalid pickle bytes prove the API reads the JSON metadata only.
    (state_dir / "state.pkl").write_bytes(b"not a pickle")
    backups = cfg.paths.data_root / "state" / "SE3" / "ensemble_hourly_exp.backups"
    (backups / "2026-10-02").mkdir(parents=True)
    (backups / "2026-10-05").mkdir()

    _write_actuals(cfg, _actual_frame(target_time, 10.0, 6.0))
    client = TestClient(create_app(cfg))

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "env": "dev", "git_sha": "abc123"}

    zones = client.get("/api/zones").json()
    assert [item["zone"] for item in zones] == ["SE1", "SE2", "SE3", "SE4"]
    by_zone = {item["zone"]: item for item in zones}
    assert by_zone["SE3"]["latest_origin"] == today.isoformat()
    assert by_zone["SE3"]["stale"] is False
    assert by_zone["SE3"]["last_fit"] == "2026-10-01"
    assert by_zone["SE3"]["challenger"] == {
        "latest_origin": today.isoformat(),
        "model_version": "17",
    }
    assert by_zone["SE2"]["stale"] is True
    assert by_zone["SE1"]["latest_origin"] is None and by_zone["SE1"]["stale"] is True
    assert by_zone["SE1"]["challenger"] is None

    origins = client.get("/api/zones/SE3/origins").json()
    assert origins == {"zone": "SE3", "origins": [today.isoformat(), old_origin.isoformat()]}

    forecast = client.get("/api/zones/SE3/forecast").json()
    assert forecast["origin_date"] == today.isoformat()
    assert forecast["model_version"] == "17"
    assert forecast["quantiles"] == QUANTILES
    assert forecast["hours"][0]["hour_local"] == 12
    assert forecast["hours"][0]["target_time"] == target_time.tz_convert(TZ).isoformat()
    assert forecast["hours"][0]["actual"] == 10.0
    assert forecast["hours"][0]["naive_7d"] == 6.0
    assert client.get(f"/api/zones/SE3/forecast?origin={old_origin}").status_code == 200
    assert client.get("/api/zones/SE3/forecast?origin=2020-01-01").status_code == 404
    challenger = client.get("/api/zones/SE3/forecast?role=challenger").json()
    assert challenger["origin_date"] == today.isoformat()
    assert challenger["model"] == "ensemble_hourly_exp_tfm3"
    assert challenger["role"] == "challenger"
    assert client.get("/api/zones/SE3/origins?role=challenger").json()["origins"] == [
        today.isoformat()
    ]

    performance = client.get("/api/zones/SE3/performance?days=30").json()
    live = performance["live"]
    assert performance["n_origins_scored"] == 1
    assert performance["daily"][0]["origin_date"] == today.isoformat()
    assert live["pinball"] == pytest.approx(5.4 / 7)
    assert live["naive_7d_pinball"] == pytest.approx(2.0)
    assert live["skill"] == pytest.approx(1.0 - (5.4 / 7) / 2.0)
    assert live["coverage_50"] == 1.0
    assert live["coverage_90"] == 1.0
    assert live["mae_median"] == pytest.approx(2.0)
    assert performance["backtest"] == {"pinball": 5.25, "naive_7d_pinball": 11.61}
    assert performance["daily"][0]["naive_7d_pinball"] == pytest.approx(2.0)
    challenger_performance = client.get("/api/zones/SE3/performance?days=30&role=challenger").json()
    assert challenger_performance["role"] == "challenger"
    assert challenger_performance["n_origins_scored"] == 1
    assert challenger_performance["backtest"] == {}

    model = client.get("/api/zones/SE3/model").json()
    assert model["model_version"] == "17"
    assert model["spec"] == "ensemble_hourly_exp"
    assert model["origins_served"] == 9
    assert model["datasets"] == {"train": "train-v1", "eval": "eval-v1", "live": "live-v1"}
    assert model["backups"] == ["2026-10-05", "2026-10-02"]
    assert client.get("/api/zones/NO1/origins").status_code == 404
    assert client.get("/api/zones/NO1/forecast").status_code == 404
    assert client.get("/api/zones/NO1/performance").status_code == 404
    assert client.get("/api/zones/NO1/model").status_code == 404
    assert client.get("/api/zones/SE3/forecast?origin=not-a-date").status_code == 422
    assert client.get("/api/zones/SE3/performance?days=0").status_code == 422


@pytest.mark.parametrize(
    ("target_date", "start_utc", "n_hours", "repeated_hour", "missing_hour"),
    [
        (date(2026, 10, 25), "2026-10-24T22:00:00Z", 25, 2, None),
        (date(2026, 3, 29), "2026-03-28T23:00:00Z", 23, None, 2),
    ],
)
def test_forecast_hours_follow_stockholm_dst(
    cfg: BaseConfig,
    monkeypatch: pytest.MonkeyPatch,
    target_date: date,
    start_utc: str,
    n_hours: int,
    repeated_hour: int | None,
    missing_hour: int | None,
) -> None:
    origin = target_date - timedelta(days=1)
    target_times = pd.date_range(start_utc, periods=n_hours, freq="h")
    _write_forecast(cfg, "SE3", origin, target_times)
    response = TestClient(create_app(cfg)).get(
        f"/api/zones/SE3/forecast?origin={origin.isoformat()}"
    )
    assert response.status_code == 200
    hours = response.json()["hours"]
    local_hours = [hour["hour_local"] for hour in hours]
    assert len(hours) == n_hours
    assert {hour["target_time"][:10] for hour in hours} == {target_date.isoformat()}
    if repeated_hour is not None:
        assert local_hours.count(repeated_hour) == 2
        repeated = [hour["target_time"] for hour in hours if hour["hour_local"] == repeated_hour]
        assert repeated[0].endswith("+02:00") and repeated[1].endswith("+01:00")
    if missing_hour is not None:
        assert missing_hour not in local_hours


def test_spa_mount_fallback_and_api_only_mode(
    cfg: BaseConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A missing dist directory means API only (a local `pnpm build` must not change this test).
    monkeypatch.setenv("PRICEFC_WEB_DIST", str(tmp_path / "no-dist"))
    api_only = TestClient(create_app(cfg))
    assert api_only.get("/api/health").status_code == 200
    assert api_only.get("/").status_code == 404

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>dashboard shell</main>")
    (dist / "app.js").write_text("window.dashboard = true;")
    monkeypatch.setenv("PRICEFC_WEB_DIST", str(dist))
    _install_metrics_http(monkeypatch, with_runs=False)
    static = TestClient(create_app(cfg))
    assert static.get("/").text == "<main>dashboard shell</main>"
    assert static.get("/nested/dashboard/route").text == "<main>dashboard shell</main>"
    assert static.get("/app.js").text == "window.dashboard = true;"
    metrics = static.get("/metrics")
    assert metrics.status_code == 200
    assert metrics.text.startswith("# HELP pricefc_build_info ")
    assert static.get("/api/health").json()["status"] == "ok"
    assert static.get("/api/zones/NO1/model").status_code == 404
    assert static.get("/api/no-route").status_code == 404


def test_skill_compares_model_and_naive_on_the_same_hours() -> None:
    from pricefc.web.data import _score

    qs = [0.05, 0.25, 0.5, 0.75, 0.95]
    frame = pd.DataFrame(
        {
            "actual": [10.0, 10.0],
            "naive_7d": [12.0, None],  # second hour has no naive value
            **{f"q{round(q * 100):02d}": [10.0, 110.0] for q in qs},
        }
    )
    got = _score(frame, qs)
    assert got is not None
    # On the one paired hour the model is perfect, so skill is 1 despite the bad unpaired hour.
    assert got["skill"] == 1.0
    assert got["pinball"] > 0


class _MetricsResponse:
    def __init__(self, payload: Any = None, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self) -> Any:
        return self.payload


def _install_metrics_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prefect_up: bool = True,
    mlflow_up: bool = True,
    with_runs: bool = True,
) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    flow_ids = {
        "ingest-daily": "flow-ingest",
        "forecast-daily": "flow-forecast",
        "score-daily": "flow-score",
    }

    def post(url: str, json: dict[str, Any], timeout: int) -> _MetricsResponse:
        assert timeout == 3
        calls.append((url, json))
        if not prefect_up:
            raise ConnectionError("Prefect is down")
        if url.endswith("/flows/filter"):
            return _MetricsResponse(
                [{"name": name, "id": flow_id} for name, flow_id in flow_ids.items()]
            )
        if json.get("sort") == "NEXT_SCHEDULED_START_TIME_ASC":
            scheduled = [
                {"flow_id": "flow-ingest"},
                {"flow_id": "flow-forecast"},
                {"flow_id": "flow-forecast"},
                {"flow_id": "flow-score"},
            ]
            return _MetricsResponse(scheduled if with_runs else [])
        # History must never include future runs: they have no start time and would look newest.
        assert json["flow_runs"]["state"]["type"] == {"not_any_": ["SCHEDULED"]}
        runs = [
            {
                "flow_id": "flow-ingest",
                "state_type": "COMPLETED",
                "start_time": "2026-10-06T08:00:00+00:00",
                "end_time": "2026-10-06T08:00:12+00:00",
                "state_timestamp": "2026-10-06T08:00:12+00:00",
            },
            {
                "flow_id": "flow-forecast",
                "state_type": "FAILED",
                "start_time": "2026-10-06T09:00:00+00:00",
                "end_time": "2026-10-06T09:00:05+00:00",
                "state_timestamp": "2026-10-06T09:00:05+00:00",
            },
        ]
        return _MetricsResponse(runs if with_runs else [])

    def get(url: str, timeout: int) -> _MetricsResponse:
        assert timeout == 3
        if not mlflow_up:
            raise ConnectionError("MLflow is down")
        assert url.endswith("/health")
        return _MetricsResponse({})

    monkeypatch.setattr(web_metrics.requests, "post", post)
    monkeypatch.setattr(web_metrics.requests, "get", get)
    return calls


def _parse_prometheus(
    text: str,
) -> tuple[dict[str, dict[str, str]], dict[str, list[tuple[dict[str, str], float]]]]:
    metadata: dict[str, dict[str, str]] = {}
    samples: dict[str, list[tuple[dict[str, str], float]]] = {}
    for line in text.splitlines():
        if line.startswith("# HELP "):
            _, _, name, help_text = line.split(" ", 3)
            metadata.setdefault(name, {})["help"] = help_text
            continue
        if line.startswith("# TYPE "):
            _, _, name, metric_type = line.split(" ", 3)
            metadata.setdefault(name, {})["type"] = metric_type
            continue
        match = PROM_SAMPLE.fullmatch(line)
        assert match is not None, f"invalid exposition sample: {line}"
        name, label_text, raw_value = match.groups()
        labels = {
            key: json.loads(f'"{encoded}"') for key, encoded in PROM_LABEL.findall(label_text or "")
        }
        samples.setdefault(name, []).append((labels, float(raw_value)))
    return metadata, samples


def _metric_value(
    samples: dict[str, list[tuple[dict[str, str], float]]], name: str, **labels: str
) -> float:
    matching = [value for actual, value in samples[name] if actual == labels]
    assert len(matching) == 1, f"{name} labels {labels} matched {matching}"
    return matching[0]


def _write_raw_manifest(cfg: BaseConfig, pulled_at: str, passed: bool, key: str = "SE3") -> Path:
    snapshot = (
        cfg.paths.raw
        / "elprisetjustnu"
        / "day_ahead_prices"
        / key
        / f"pulled_at={pulled_at.replace(':', '-')}"
    )
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.json").write_text(
        json.dumps({"pulled_at": pulled_at, "validation": {"passed": passed}})
    )
    if passed:
        pd.DataFrame(
            {
                "timestamp": [pd.Timestamp("2026-10-05T00:00:00Z")],
                "price_eur_mwh": [1.0],
                "resolution": ["PT60M"],
            }
        ).to_parquet(snapshot / "part-0.parquet", index=False)
    return snapshot


def test_metrics_exposition_and_metric_families(
    cfg: BaseConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRICEFC_ENV", "production")
    monkeypatch.setenv("PRICEFC_GIT_SHA", "abc123")
    today = datetime.now(TZ).date()
    target_time = _local_time(today + timedelta(days=1), 12).tz_convert("UTC")
    _write_forecast(cfg, "SE3", today, pd.DatetimeIndex([target_time]))
    _write_forecast(
        cfg,
        "SE3",
        today,
        pd.DatetimeIndex([target_time]),
        [1.0, 3.0, 5.0, 9.0, 13.0, 17.0, 21.0],
        role="challenger",
        model="ensemble_hourly_exp_tfm3",
    )
    state_dir = cfg.paths.data_root / "state" / "SE3" / "ensemble_hourly_exp"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps({"spec": "ensemble_hourly_exp", "last_origin": today.isoformat()})
    )
    _write_raw_manifest(cfg, "2026-10-05T11:00:00+00:00", True)
    _write_raw_manifest(cfg, "2026-10-06T11:00:00+00:00", False)
    _write_actuals(cfg, _actual_frame(target_time, 10, 6))
    scores_dir = cfg.paths.data_root / "scores" / "SE3"
    scores_dir.mkdir(parents=True)
    pd.DataFrame({"origin_date": [today.isoformat()]}).to_parquet(
        scores_dir / "champion.parquet", index=False
    )
    _install_metrics_http(monkeypatch)

    response = TestClient(create_app(cfg)).get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "version=0.0.4" in response.headers["content-type"]
    metadata, samples = _parse_prometheus(response.text)
    assert set(metadata) == set(web_metrics._METRIC_DEFINITIONS)
    assert all(entry.get("help") and entry.get("type") == "gauge" for entry in metadata.values())

    assert _metric_value(samples, "pricefc_build_info", env="production", git_sha="abc123") == 1
    expected_origin = _local_time(today, 9).timestamp()
    assert _metric_value(
        samples,
        "pricefc_forecast_last_origin_timestamp_seconds",
        zone="SE3",
        role="champion",
    ) == pytest.approx(expected_origin)
    assert _metric_value(
        samples,
        "pricefc_forecast_target_date_timestamp_seconds",
        zone="SE3",
        role="challenger",
    ) == pytest.approx(_local_time(today + timedelta(days=1), 0).timestamp())
    assert _metric_value(samples, "pricefc_forecast_rows", zone="SE3", role="challenger") == 1
    assert _metric_value(
        samples, "pricefc_score_last_origin_timestamp_seconds", zone="SE3", role="champion"
    ) == pytest.approx(_local_time(today, 0).timestamp())
    assert _metric_value(samples, "pricefc_model_version", zone="SE3", role="champion") == 17
    assert _metric_value(
        samples,
        "pricefc_state_last_origin_timestamp_seconds",
        zone="SE3",
        model="ensemble_hourly_exp",
    ) == pytest.approx(expected_origin)
    assert (
        _metric_value(samples, "pricefc_scored_days", zone="SE3", role="champion", window="7d") == 1
    )
    assert _metric_value(samples, "pricefc_backtest_pinball", zone="SE3") == 5.25
    raw_labels = {"source": "elprisetjustnu", "dataset": "day_ahead_prices", "key": "SE3"}
    assert _metric_value(
        samples,
        "pricefc_raw_latest_valid_pulled_at_timestamp_seconds",
        **raw_labels,
    ) == pytest.approx(pd.Timestamp("2026-10-05T11:00:00+00:00").timestamp())
    assert _metric_value(samples, "pricefc_raw_latest_pull_valid", **raw_labels) == 0
    assert _metric_value(samples, "pricefc_prefect_up") == 1
    assert _metric_value(samples, "pricefc_mlflow_up") == 1
    assert _metric_value(samples, "pricefc_flow_runs_scheduled", flow="forecast-daily") == 2
    assert _metric_value(samples, "pricefc_flow_runs_scheduled", flow="score-daily") == 1
    assert (
        _metric_value(samples, "pricefc_flow_last_run_state", flow="forecast-daily", state="failed")
        == 1
    )
    assert (
        _metric_value(samples, "pricefc_flow_last_success_duration_seconds", flow="ingest-daily")
        == 12
    )


def test_metrics_prefect_down_is_partial_and_never_500(
    cfg: BaseConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_metrics_http(monkeypatch, prefect_up=False, mlflow_up=True)
    response = TestClient(create_app(cfg)).get("/metrics")
    assert response.status_code == 200
    _, samples = _parse_prometheus(response.text)
    assert _metric_value(samples, "pricefc_prefect_up") == 0
    assert _metric_value(samples, "pricefc_mlflow_up") == 1
    assert _metric_value(samples, "pricefc_metrics_errors", section="prefect") == 1
    assert "pricefc_flow_runs_scheduled" not in samples


def test_metrics_render_is_cached_for_sixty_seconds(
    cfg: BaseConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = datetime.now(TZ).date()
    target_time = _local_time(today + timedelta(days=1), 12).tz_convert("UTC")
    path = _write_forecast(cfg, "SE3", today, pd.DatetimeIndex([target_time]))
    calls = _install_metrics_http(monkeypatch, with_runs=False)
    fake_clock = [100.0]
    monkeypatch.setattr(web_metrics.time, "monotonic", lambda: fake_clock[0])
    client = TestClient(create_app(cfg))

    first = client.get("/metrics")
    first_metadata, first_samples = _parse_prometheus(first.text)
    assert _metric_value(first_samples, "pricefc_forecast_rows", zone="SE3", role="champion") == 1
    calls_after_first = len(calls)

    target_times = pd.DatetimeIndex([target_time, target_time + pd.Timedelta(hours=1)])
    _write_forecast(cfg, "SE3", today, target_times)
    second = client.get("/metrics")
    _, second_samples = _parse_prometheus(second.text)
    assert second.text == first.text
    assert _metric_value(second_samples, "pricefc_forecast_rows", zone="SE3", role="champion") == 1
    assert len(calls) == calls_after_first

    fake_clock[0] += 61
    third = client.get("/metrics")
    _, third_samples = _parse_prometheus(third.text)
    assert _metric_value(third_samples, "pricefc_forecast_rows", zone="SE3", role="champion") == 2
    assert len(calls) > calls_after_first
    assert "pricefc_metrics_render_seconds" in first_metadata
    assert path.is_file()


def test_metrics_handles_broken_forecast_file(
    cfg: BaseConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = datetime.now(TZ).date()
    broken = cfg.paths.data_root / "forecasts" / "SE3" / f"{today.isoformat()}.parquet"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not a parquet file")
    broken_score = cfg.paths.data_root / "scores" / "SE3" / "champion.parquet"
    broken_score.parent.mkdir(parents=True)
    broken_score.write_bytes(b"not a parquet file")
    _install_metrics_http(monkeypatch)

    response = TestClient(create_app(cfg)).get("/metrics")
    assert response.status_code == 200
    _, samples = _parse_prometheus(response.text)
    assert _metric_value(samples, "pricefc_metrics_errors", section="forecasts") == 1
    assert _metric_value(samples, "pricefc_metrics_errors", section="quality") >= 1
    assert _metric_value(samples, "pricefc_metrics_errors", section="scores") == 1
