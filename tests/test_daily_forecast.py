from datetime import date
from pathlib import Path
from typing import Any

import pytest

from pricefc.config import BaseConfig, load_config
from pricefc.flows import daily


@pytest.fixture
def cfg(tmp_path: Path) -> BaseConfig:
    root = tmp_path / "data"
    return load_config(
        Path("configs/base.yaml"),
        {
            "zones": ["SE3", "SE4"],
            "paths": {"data_root": str(root), "raw": str(root / "raw"), "datasets": str(root)},
            "mlflow": {
                "tracking_uri": f"sqlite:///{tmp_path}/mlflow.db",
                "artifact_root": str(tmp_path / "artifacts"),
            },
        },
    )


def _install_flow_fakes(
    monkeypatch: pytest.MonkeyPatch,
    cfg: BaseConfig,
    *,
    missing_alias_zone: str | None = None,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    monkeypatch.setattr(daily, "_base", lambda: cfg)
    monkeypatch.setattr(daily, "ingest_prices_task", lambda: {"failed": 0})
    monkeypatch.setattr(daily, "ingest_weather_task", lambda _endpoint: {"failed": 0})
    monkeypatch.setattr(daily, "build_datasets_task", lambda zone: {"true_lead": zone})
    monkeypatch.setattr(daily, "_build_silver_best_effort", lambda: {"tables": 0})
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "pricefc.models.timesfm.release_backends", lambda: calls.append(("release", "-"))
    )
    alerts: list[tuple[str, str]] = []

    def fake_forecast(zone: str, _day: date, _model: str, alias: str, role: str) -> dict[str, str]:
        calls.append((role, zone))
        if role == "challenger" and zone == "SE3":
            raise RuntimeError("injected challenger failure")
        return {"version": f"{role}-{zone}", "alias": alias}

    monkeypatch.setattr(daily, "forecast_zone_task", fake_forecast)

    def resolve_optional(_zone: str, _alias: str, *, tracking_uri: str | None = None) -> str | None:
        assert tracking_uri == cfg.mlflow.tracking_uri
        return None if _zone == missing_alias_zone else "2"

    monkeypatch.setattr("pricefc.serving.registry.resolve_optional", resolve_optional)
    monkeypatch.setattr(
        daily,
        "_alert_challenger_failure",
        lambda zone, _day, alias, _exc: alerts.append((zone, alias)),
    )
    return calls, alerts


def test_champions_all_run_before_challenger_and_failure_is_isolated(
    cfg: BaseConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, alerts = _install_flow_fakes(monkeypatch, cfg, missing_alias_zone="SE4")

    result = daily.forecast_daily.fn(date(2026, 10, 6))

    # Champion checkpoints are released before any challenger loads its own.
    assert calls == [
        ("champion", "SE3"),
        ("champion", "SE4"),
        ("release", "-"),
        ("challenger", "SE3"),
    ]
    assert alerts == [("SE3", "challenger")]
    assert result["errors"] == {}
    assert result["zones"]["SE3"]["version"] == "champion-SE3"
    assert result["challengers"] == {
        "SE3:challenger": {"error": "RuntimeError: injected challenger failure"}
    }


def test_missing_challenger_alias_is_skipped_for_that_zone(
    cfg: BaseConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, alerts = _install_flow_fakes(monkeypatch, cfg, missing_alias_zone="SE3")

    result: dict[str, Any] = daily.forecast_daily.fn(date(2026, 10, 6))

    assert calls == [
        ("champion", "SE3"),
        ("champion", "SE4"),
        ("release", "-"),
        ("challenger", "SE4"),
    ]
    assert alerts == []
    assert "SE3:challenger" not in result["challengers"]
    assert result["errors"] == {}
