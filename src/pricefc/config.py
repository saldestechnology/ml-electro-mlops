"""Configuration loading, validation and hashing."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

Zone = Literal["SE1", "SE2", "SE3", "SE4"]
Resolution = Literal["hourly", "PT15M"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OriginConfig(_Strict):
    local_time: time


class PathsConfig(_Strict):
    data_root: Path
    raw: Path
    datasets: Path


class MlflowConfig(_Strict):
    tracking_uri: str
    artifact_root: str


class ServedModelConfig(_Strict):
    model: str
    alias: str
    role: Literal["champion", "challenger"]


class ServingConfig(_Strict):
    models: list[ServedModelConfig] = Field(min_length=1)

    @field_validator("models")
    @classmethod
    def _unique_aliases_and_champion(cls, v: list[ServedModelConfig]) -> list[ServedModelConfig]:
        aliases = [model.alias for model in v]
        if len(set(aliases)) != len(aliases):
            raise ValueError("served model aliases must be unique")
        champions = [model for model in v if model.role == "champion"]
        if len(champions) != 1 or champions[0].alias != "champion":
            raise ValueError("serving.models must contain exactly one champion alias")
        return v


class BaseConfig(_Strict):
    zones: list[Zone] = Field(min_length=1)
    resolution: Resolution
    timezone: str
    origin: OriginConfig
    quantiles: list[float] = Field(min_length=1)
    history_start: date
    seed: int
    paths: PathsConfig
    mlflow: MlflowConfig
    licence_policy: Literal["deployable_only", "noncommercial_ok"] = "deployable_only"
    serving: ServingConfig
    compute_env: str

    @field_validator("quantiles")
    @classmethod
    def _check_quantiles(cls, v: list[float]) -> list[float]:
        if any(not 0.0 < q < 1.0 for q in v):
            raise ValueError("quantiles must lie strictly between 0 and 1")
        if sorted(set(v)) != v:
            raise ValueError("quantiles must be unique and sorted ascending")
        if 0.5 not in v:
            raise ValueError("quantiles must include 0.5 (the point forecast)")
        return v

    @field_validator("zones")
    @classmethod
    def _unique_zones(cls, v: list[Zone]) -> list[Zone]:
        if len(set(v)) != len(v):
            raise ValueError("zones must be unique")
        return v


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


ENV_CONFIG_VAR = "PRICEFC_ENV_CONFIG"


def load_config(path: Path, overrides: dict[str, Any] | None = None) -> BaseConfig:
    """Load the base config, then the deployment overlay named by $PRICEFC_ENV_CONFIG (e.g.
    configs/envs/production.yaml: paths, zones, tracking server), then explicit overrides."""
    raw = load_yaml(path)
    env_overlay = os.environ.get(ENV_CONFIG_VAR)
    if env_overlay:
        raw = _deep_merge(raw, load_yaml(Path(env_overlay)))
    if overrides:
        raw = _deep_merge(raw, overrides)
    return BaseConfig.model_validate(raw)


def config_hash(config: BaseModel) -> str:
    """Stable short hash of a resolved config (logged as the `config_hash` tag)."""
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


# --- ingestion -------------------------------------------------------------------------------


class OpenMeteoEndpoint(_Strict):
    url: str
    start: date | None = None
    lead_days: list[int] = Field(default_factory=list)
    forecast_days: int | None = None
    run_interval_hours: int | None = None  # single_runs: spacing of model initialisations
    max_null_frac: float = Field(default=0.0, ge=0, le=1)


class OpenMeteoConfig(_Strict):
    model: str
    variables: list[str] = Field(min_length=1)
    wind_speed_unit: Literal["ms", "kmh", "mph", "kn"]
    min_interval_s: float = Field(ge=0)
    max_retries: int = Field(ge=0)
    timeout_s: float = Field(gt=0)
    endpoints: dict[
        Literal["historical_forecast", "previous_runs", "single_runs", "forecast"],
        OpenMeteoEndpoint,
    ]


EntsoeDataset = Literal[
    "day_ahead_prices",
    "load_forecast",
    "wind_solar_forecast",
    "generation",
    "crossborder_flows",
    "scheduled_exchanges",
    "generation_unavailability",
    "hydro_reservoirs",
]


class EntsoeConfig(_Strict):
    min_interval_s: float = Field(ge=0)
    max_retries: int = Field(ge=0)
    chunk_days: int = Field(gt=0, le=366)
    history_start: date
    datasets: list[EntsoeDataset]
    neighbour_price_areas: list[str]
    hydro_reservoir_area: str


class PricesConfig(_Strict):
    source: Literal["elprisetjustnu", "entsoe"]


class ElprisConfig(_Strict):
    url_template: str
    start: date
    min_interval_s: float = Field(ge=0)
    max_retries: int = Field(ge=0)
    timeout_s: float = Field(gt=0)
    user_agent: str
    # Days whose source data is known to be wrong; dropped on ingest, never repaired.
    known_bad_days: dict[Zone, list[date]] = Field(default_factory=dict)


class IngestConfig(_Strict):
    prices: PricesConfig
    elprisetjustnu: ElprisConfig
    open_meteo: OpenMeteoConfig
    entsoe: EntsoeConfig


def load_ingest_config(path: Path) -> IngestConfig:
    return IngestConfig.model_validate(load_yaml(path))


# --- features --------------------------------------------------------------------------------


class Location(_Strict):
    name: str = Field(pattern=r"^[a-z0-9_]+$")
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    role: Literal["demand", "wind", "solar", "neighbour_wind"]


class WeatherFeatures(_Strict):
    locations: dict[Zone, list[Location]]

    @field_validator("locations")
    @classmethod
    def _unique_names(cls, v: dict[Zone, list[Location]]) -> dict[Zone, list[Location]]:
        names = [loc.name for locs in v.values() for loc in locs]
        if len(names) != len(set(names)):
            raise ValueError("location names must be unique across zones")
        return v


class WeatherSourceConfig(_Strict):
    endpoint: Literal["historical_forecast", "previous_runs"]
    lead_days: int = Field(ge=1, le=7)
    column_suffix: str
    # Live reconstruction of the same lead for hours the archive does not hold yet.
    live_endpoint: Literal["single_runs"] | None = None


class DatasetWeatherConfig(_Strict):
    model: str
    preceding_hour_variables: list[str]
    direction_variables: list[str]
    publication_delay_hours: float = Field(ge=0)
    sources: dict[Literal["stitched", "true_lead"], WeatherSourceConfig]


class LeakageAuditConfig(_Strict):
    n_origins: int = Field(ge=1)
    seed: int


class DatasetConfig(_Strict):
    price_publication_time: time
    price_lag_days: list[int] = Field(min_length=1)
    same_hour_mean_days: int = Field(ge=1)
    rolling_windows_hours: list[int] = Field(min_length=1)
    neighbour_price_zones: dict[Zone, list[Zone]]
    weather: DatasetWeatherConfig
    leakage_audit: LeakageAuditConfig

    @field_validator("price_lag_days")
    @classmethod
    def _lags_known_at_origin(cls, v: list[int]) -> list[int]:
        # Lag 0 would be the target day itself, which is never published before the origin.
        if min(v) < 1:
            raise ValueError("price lags must be >= 1 day")
        return v


class FeaturesConfig(_Strict):
    weather: WeatherFeatures
    dataset: DatasetConfig


def load_features_config(path: Path) -> FeaturesConfig:
    return FeaturesConfig.model_validate(load_yaml(path))


# --- backtest --------------------------------------------------------------------------------


class BootstrapConfig(_Strict):
    n_resamples: int = Field(ge=100)
    block_length: int = Field(ge=1)
    level: float = Field(gt=0, lt=1)
    seed: int


class DMConfig(_Strict):
    horizon: int = Field(ge=1)


class BacktestConfig(_Strict):
    eval_start: date | None = None
    eval_end: date | None = None
    dev_every_n_days: int = Field(ge=1)
    train_start: date | None = None
    reference_model: str
    models: list[str] = Field(min_length=1)
    bootstrap: BootstrapConfig
    dm: DMConfig

    @field_validator("dev_every_n_days")
    @classmethod
    def _not_weekly(cls, v: int) -> int:
        if v % 7 == 0:
            raise ValueError("dev_every_n_days must not be a multiple of 7 (one weekday only)")
        return v


def load_backtest_config(path: Path) -> BacktestConfig:
    return BacktestConfig.model_validate(load_yaml(path))


def load_model_params(models_dir: Path) -> dict[str, dict[str, Any]]:
    """Merge every configs/models/**/*.yaml into {model_name: params}."""
    params: dict[str, dict[str, Any]] = {}
    for f in sorted(models_dir.rglob("*.yaml")):
        for name, p in load_yaml(f).items():
            if name in params:
                raise ValueError(f"model {name!r} defined twice")
            params[name] = dict(p or {})
    return params
