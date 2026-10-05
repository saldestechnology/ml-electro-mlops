"""Prefect flow wiring (no server, no network)."""

from typing import Any

import pytest

pytest.importorskip("prefect")

from pricefc.flows import daily


def test_deployments_are_scheduled_in_stockholm_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRICEFC_CRON_MORNING", "0 8 * * *")
    deps = daily.deployments("staging")
    assert [d.name for d in deps] == ["staging-morning", "staging-afternoon"]
    crons = [d.schedules[0].schedule for d in deps]
    assert crons[0].cron == "0 8 * * *" and crons[1].cron == "30 13 * * *"
    assert all(c.timezone == "Europe/Stockholm" for c in crons)
    assert all(d.tags == ["staging"] for d in deps)


def test_ingest_flow_fails_loudly_on_invalid_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake(kind: str, failed: int) -> Any:
        def run(*args: Any, **kwargs: Any) -> dict[str, int]:
            calls.append(kind if not args else f"{kind}:{args[0]}")
            return {"snapshots": 4, "failed": failed}

        return run

    monkeypatch.setattr(daily, "ingest_prices_task", fake("prices", 0))
    monkeypatch.setattr(daily, "ingest_weather_task", fake("weather", 0))
    out = daily.ingest_daily.fn()
    assert set(out) == {"prices", "previous_runs", "historical_forecast", "forecast"}
    assert calls == [
        "prices",
        "weather:previous_runs",
        "weather:historical_forecast",
        "weather:forecast",
    ]

    monkeypatch.setattr(daily, "ingest_weather_task", fake("weather", 1))
    with pytest.raises(RuntimeError, match="validation failed"):
        daily.ingest_daily.fn()
