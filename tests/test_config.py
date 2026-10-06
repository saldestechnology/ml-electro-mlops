from pathlib import Path

import pytest
from pydantic import ValidationError

from pricefc.config import config_hash, load_config

BASE = Path("configs/base.yaml")


def test_base_config_loads() -> None:
    cfg = load_config(BASE)
    assert cfg.zones
    assert 0.5 in cfg.quantiles
    assert cfg.licence_policy == "deployable_only"
    assert [(model.model, model.alias, model.role) for model in cfg.serving.models] == [
        ("ensemble_hourly_exp", "champion", "champion"),
        ("ensemble_hourly_exp_tfm3", "challenger", "challenger"),
    ]


def test_config_hash_stable_and_sensitive() -> None:
    a = load_config(BASE)
    assert config_hash(a) == config_hash(load_config(BASE))
    b = load_config(BASE, {"seed": a.seed + 1})
    assert config_hash(a) != config_hash(b)


@pytest.mark.parametrize(
    "override",
    [
        {"zones": ["SE5"]},
        {"zones": ["SE3", "SE3"]},
        {"quantiles": [0.1, 0.9]},
        {"quantiles": [0.9, 0.5, 0.1]},
        {"resolution": "daily"},
        {"licence_policy": "always"},
        {"unknown_key": 1},
    ],
)
def test_invalid_config_rejected(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        load_config(BASE, override)


def test_env_overlay_is_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    from pricefc.config import ENV_CONFIG_VAR

    monkeypatch.setenv(ENV_CONFIG_VAR, "configs/envs/production.yaml")
    cfg = load_config(Path("configs/base.yaml"))
    assert cfg.zones == ["SE1", "SE2", "SE3", "SE4"]
    assert str(cfg.paths.raw) == "/data/raw"
    assert cfg.mlflow.tracking_uri == "http://localhost:5000"
    assert cfg.licence_policy == "noncommercial_ok"
    assert cfg.timezone == "Europe/Stockholm"  # untouched keys come from base
