"""Prefect flow wiring (no server, no network)."""

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("prefect")

from pricefc.flows import daily


def test_deployments_are_scheduled_in_stockholm_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRICEFC_CRON_MORNING", "0 8 * * *")
    monkeypatch.delenv("PRICEFC_CRON_FORECAST", raising=False)
    monkeypatch.delenv("PRICEFC_CRON_SCORE", raising=False)
    deps = daily.deployments("staging")
    assert [d.name for d in deps] == [
        "staging-morning",
        "staging-afternoon",
        "staging-forecast",
        "staging-score",
    ]
    crons = [d.schedules[0].schedule for d in deps]
    assert crons[0].cron == "0 8 * * *" and crons[1].cron == "30 13 * * *"
    assert crons[2].cron == "5 9 * * *"
    assert crons[3].cron == "0 14,17 * * *"
    assert deps[2].flow_name == "forecast-daily"
    assert deps[3].flow_name == "score-daily"
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
    monkeypatch.setattr(daily, "_build_silver_best_effort", lambda: {"built": []})
    out = daily.ingest_daily.fn()
    assert set(out) == {"prices", "previous_runs", "historical_forecast", "forecast", "silver"}
    assert calls == [
        "prices",
        "weather:previous_runs",
        "weather:historical_forecast",
        "weather:forecast",
    ]

    monkeypatch.setattr(daily, "ingest_weather_task", fake("weather", 1))
    with pytest.raises(RuntimeError, match="validation failed"):
        daily.ingest_daily.fn()


def test_forecast_flow_runs_every_zone_and_fails_at_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = SimpleNamespace(
        zones=["SE3", "SE4", "SE1"],
        timezone="Europe/Stockholm",
        serving=SimpleNamespace(
            models=[
                SimpleNamespace(model="ensemble_hourly_exp", alias="champion", role="champion"),
                SimpleNamespace(
                    model="ensemble_hourly_exp_tfm3", alias="challenger", role="challenger"
                ),
            ]
        ),
    )
    monkeypatch.setattr(daily, "_base", lambda: base)
    calls: list[str] = []

    def ingest(*args: Any) -> dict[str, int]:
        calls.append(f"ingest:{args[0] if args else 'prices'}")
        return {"snapshots": 1, "failed": 0}

    def build(zone: str) -> dict[str, str]:
        calls.append(f"build:{zone}")
        return {"stitched": "s", "true_lead": "t"}

    def forecast(zone: str, day: date, _model: str, _alias: str, role: str) -> dict[str, Any]:
        calls.append(f"forecast:{role}:{zone}:{day}")
        if role == "champion" and zone == "SE4":
            raise ValueError("live columns differ\nlong detail")
        return {"version": "1", "rows": 24, "caught_up": [], "path": "p"}

    monkeypatch.setattr(daily, "ingest_prices_task", ingest)
    monkeypatch.setattr(daily, "ingest_weather_task", ingest)
    monkeypatch.setattr(
        daily,
        "_build_silver_best_effort",
        lambda: calls.append("silver") or {"error": "build failed"},
    )
    monkeypatch.setattr(daily, "build_datasets_task", build)
    monkeypatch.setattr(daily, "forecast_zone_task", forecast)
    monkeypatch.setattr("pricefc.serving.registry.resolve_optional", lambda *_, **__: None)
    day = date(2026, 10, 6)
    with pytest.raises(RuntimeError, match=r"SE4.*ValueError: live columns differ") as err:
        daily.forecast_daily.fn(day)
    assert "long detail" not in str(err.value)
    assert calls == [
        "ingest:prices",
        "ingest:previous_runs",
        "ingest:historical_forecast",
        "ingest:single_runs",
        "silver",
        "build:SE3",
        "build:SE4",
        "build:SE1",
        f"forecast:champion:SE3:{day}",
        f"forecast:champion:SE4:{day}",
        f"forecast:champion:SE1:{day}",
    ]

    monkeypatch.setattr(
        daily,
        "forecast_zone_task",
        lambda z, d, _m, _a, _r: {"version": "1"},
    )
    out = daily.forecast_daily.fn(day)
    assert set(out["zones"]) == {"SE3", "SE4", "SE1"} and out["errors"] == {}
    assert out["silver"] == {"error": "build failed"}


def test_score_flow_reports_ingest_failure_and_still_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fail_ingest() -> dict[str, Any]:
        calls.append("ingest")
        raise OSError("price API unavailable")

    monkeypatch.setattr(daily, "ingest_prices_task", fail_ingest)
    monkeypatch.setattr(
        daily, "score_forecasts_task", lambda: calls.append("score") or [{"origin_date": "D"}]
    )
    out = daily.score_daily.fn()
    assert calls == ["ingest", "score"]
    assert out["ingest"] == {"error": "OSError: price API unavailable"}
    assert out["scored"] == 1


def test_score_flow_reports_invalid_ingest_and_scoring_errors_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daily, "ingest_prices_task", lambda: {"snapshots": 1, "failed": 1})
    monkeypatch.setattr(daily, "score_forecasts_task", lambda: [])
    out = daily.score_daily.fn()
    assert out["ingest"]["warning"] == "1 invalid price snapshot(s)"
    assert out["scored"] == 0

    monkeypatch.setattr(
        daily, "score_forecasts_task", lambda: (_ for _ in ()).throw(ValueError("score broke"))
    )
    with pytest.raises(ValueError, match="score broke"):
        daily.score_daily.fn()


def test_failure_hook_alerts_with_a_short_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    from pricefc import alerts

    sent: list[str] = []
    monkeypatch.setattr(alerts, "send_telegram", lambda text: sent.append(text) or True)
    assert daily.forecast_daily.on_failure_hooks == [daily._alert]
    assert daily.ingest_daily.on_crashed_hooks == [daily._alert]
    assert daily.score_daily.on_failure_hooks == [daily._alert]
    assert daily.score_daily.on_crashed_hooks == [daily._alert]
    state = SimpleNamespace(
        name="Failed", message="Traceback...\nFlow run encountered an exception: RuntimeError: x"
    )
    run = SimpleNamespace(name="brave-fox", id="abc-123")
    daily._alert(daily.forecast_daily, run, state)
    assert sent == [
        "forecast-daily Failed: run brave-fox (abc-123)\n"
        "Flow run encountered an exception: RuntimeError: x"
    ]

    def boom(text: str) -> bool:
        raise OSError("no network")

    monkeypatch.setattr(alerts, "send_telegram", boom)
    daily._alert(daily.forecast_daily, run, state)  # never raises
