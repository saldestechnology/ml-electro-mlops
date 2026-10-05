import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from pricefc.config import Location, load_config, load_ingest_config
from pricefc.ingest import throttle
from pricefc.ingest.openmeteo import OpenMeteoClient, year_chunks
from pricefc.ingest.run import ingest_weather
from pricefc.ingest.throttle import RateLimiter

FIX = Path(__file__).parent / "fixtures" / "open_meteo"
CFG = load_ingest_config(Path("configs/ingest.yaml")).open_meteo
STOCKHOLM = Location(name="se3_stockholm", lat=59.33, lon=18.07, role="demand")


class FakeResponse:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> dict[str, Any]:
        return self._body


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any], timeout: float) -> FakeResponse:
        self.calls.append({"url": url, **params})
        return self.responses.pop(0)


def fixture(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((FIX / name).read_text())
    return body


def client(responses: list[FakeResponse]) -> tuple[OpenMeteoClient, FakeSession]:
    session = FakeSession(responses)
    return OpenMeteoClient(CFG, session=session, limiter=RateLimiter(0)), session  # type: ignore[arg-type]


def test_year_chunks() -> None:
    assert year_chunks(date(2023, 11, 1), date(2025, 2, 1)) == [
        (date(2023, 11, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 2, 1)),
    ]


def test_previous_runs_variable_names() -> None:
    c, _ = client([])
    names = c.hourly_variables("previous_runs")
    assert "temperature_2m_previous_day1" in names and "snowfall_previous_day2" in names
    assert len(names) == 2 * len(CFG.variables)


def test_fetch_recorded_historical_forecast() -> None:
    c, session = client(
        [FakeResponse(200, fixture("historical_forecast_stockholm_2025-10-25_27.json"))]
    )
    pull = c.fetch_range("historical_forecast", STOCKHOLM, date(2025, 10, 25), date(2025, 10, 27))
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["models"] == "ecmwf_ifs" and call["timeformat"] == "unixtime"
    assert pull.dataset == "historical_forecast__ecmwf_ifs"
    df = pull.data
    assert str(df["timestamp"].dt.tz) == "UTC" and len(df) == 72
    assert df["timestamp"].iloc[0] == pd.Timestamp("2025-10-25", tz="UTC")
    assert pull.requested_end == pd.Timestamp("2025-10-28", tz="UTC")


def test_retries_on_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(throttle.time, "sleep", sleeps.append)
    c, session = client(
        [
            FakeResponse(429, {"reason": "slow down"}),
            FakeResponse(200, fixture("previous_runs_stockholm_2025-10-25_27.json")),
        ]
    )
    pull = c.fetch_range("previous_runs", STOCKHOLM, date(2025, 10, 25), date(2025, 10, 27))
    assert len(session.calls) == 2 and sleeps == [2.0]
    assert "wind_speed_100m_previous_day2" in pull.data.columns


def test_client_error_is_not_retried() -> None:
    c, _ = client([FakeResponse(400, {"error": True, "reason": "bad"})])
    with pytest.raises(RuntimeError, match="HTTP 400"):
        c.fetch_range("historical_forecast", STOCKHOLM, date(2025, 10, 25), date(2025, 10, 27))


def test_ingest_weather_writes_valid_snapshot_on_dst_day(tmp_path: Path) -> None:
    base = load_config(
        Path("configs/base.yaml"),
        {
            "paths": {
                "data_root": str(tmp_path),
                "raw": str(tmp_path / "raw"),
                "datasets": str(tmp_path / "ds"),
            }
        },
    )
    c, _ = client([FakeResponse(200, fixture("historical_forecast_stockholm_2025-10-25_27.json"))])
    ingest = load_ingest_config(Path("configs/ingest.yaml"))
    [result] = ingest_weather(
        base,
        ingest,
        [STOCKHOLM],
        "historical_forecast",
        date(2025, 10, 25),
        date(2025, 10, 27),
        client=c,
    )
    assert result.report.passed, result.report.errors
    m = result.snapshot.manifest
    assert m["weather_source"] == "open-meteo:historical_forecast:ecmwf_ifs"
    assert m["validation"]["passed"] and m["row_count"] == 72
    assert "CC BY 4.0" in m["attribution"]


def test_lead_run_matches_previous_runs_rule() -> None:
    # Verified against the live API on 2026-10-05: previous_day2 at T = run at floor_6h(T - 48h).
    from pricefc.ingest.openmeteo import lead_run

    t = pd.Timestamp
    assert lead_run(t("2026-10-06T05:00Z"), 2, 6) == t("2026-10-04T00:00Z")
    assert lead_run(t("2026-10-06T06:00Z"), 2, 6) == t("2026-10-04T06:00Z")
    assert lead_run(t("2026-10-06T23:00Z"), 2, 6) == t("2026-10-04T18:00Z")


def _run_body(run: str, days: int) -> dict[str, Any]:
    """Every value encodes the run's hour, so the test can see which run filled each hour."""
    start = pd.Timestamp(run[:10], tz="UTC")
    times = pd.date_range(start, periods=24 * days, freq="h")
    value = float(pd.Timestamp(run).hour)
    hourly: dict[str, Any] = {"time": [int(x.timestamp()) for x in times]}
    hourly.update({v: [value] * len(times) for v in CFG.variables})
    return {"latitude": 59.3, "longitude": 18.1, "hourly": hourly}


def test_fetch_lead_runs_takes_each_hour_from_its_own_run() -> None:
    runs = ["2026-10-04T00:00", "2026-10-04T06:00", "2026-10-04T12:00", "2026-10-04T18:00"]
    c, session = client([FakeResponse(200, _run_body(r, 3)) for r in runs])
    pull = c.fetch_lead_runs(STOCKHOLM, date(2026, 10, 6), date(2026, 10, 6))
    assert [call["run"] for call in session.calls] == runs
    assert all(call["url"].startswith("https://single-runs-api") for call in session.calls)
    col = pull.data.set_index("timestamp")["temperature_2m_previous_day2"]
    assert list(col.to_numpy()) == [0.0] * 6 + [6.0] * 6 + [12.0] * 6 + [18.0] * 6
    assert set(pull.data.columns) == {"timestamp", *c.hourly_variables("single_runs")}


def test_fetch_lead_runs_leaves_unpublished_runs_null() -> None:
    missing = FakeResponse(
        400, {"error": True, "reason": "The requested model run is not available."}
    )
    responses = [FakeResponse(200, _run_body(f"2026-10-04T{h:02d}:00", 3)) for h in (0, 6, 12)]
    c, _ = client([*responses, missing])
    pull = c.fetch_lead_runs(STOCKHOLM, date(2026, 10, 6), date(2026, 10, 6))
    col = pull.data["temperature_2m_previous_day2"]
    assert col.isna().sum() == 6 and col.iloc[:18].notna().all()
    assert pull.chunks[-1] == {"lead_days": 2, "run": "2026-10-04T18:00", "available": False}
