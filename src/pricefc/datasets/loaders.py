"""Raw snapshots -> TimedSources (hourly prices, per-location weather) with lineage."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pandas as pd

from pricefc.config import BaseConfig, DatasetConfig, Location
from pricefc.datasets.sources import TimedSource, day_ahead_price_rule, fixed_lead_rule
from pricefc.ingest.snapshot import Snapshot, load_latest
from pricefc.timeutils import aggregate_to_hourly

WeatherKind = Literal["stitched", "true_lead"]


def load_hourly_prices(
    raw_root: Path,
    price_source: str,
    zone: str,
    base: BaseConfig,
    cfg: DatasetConfig,
    name: str,
) -> tuple[TimedSource, list[Snapshot]]:
    """Hourly mean prices (15-minute MTUs averaged), with the day-ahead publication rule."""
    if base.resolution != "hourly":
        raise NotImplementedError("only hourly datasets are implemented (D2)")
    raw, snaps = load_latest(raw_root, price_source, "day_ahead_prices", zone)
    if raw.empty:
        raise FileNotFoundError(f"no valid {price_source} price snapshots for {zone}")
    hourly = aggregate_to_hourly(raw.set_index("timestamp"), ["price_eur_mwh"])
    expected = hourly["source_resolution"].map({"PT60M": 1, "PT15M": 4})
    incomplete = hourly["n_periods"] != expected
    if incomplete.any():
        bad = hourly.index[incomplete][:5].tolist()
        raise ValueError(f"{zone}: incomplete hours after aggregation, e.g. {bad}")
    data = pd.DataFrame(
        {
            "price": hourly["price_eur_mwh"],
            "n_periods": hourly["n_periods"].astype("float64"),
            "is_pt15m": (hourly["source_resolution"] == "PT15M").astype("float64"),
        },
        index=hourly.index,
    )
    data.index.name = "valid_time"
    rule = day_ahead_price_rule(base.timezone, cfg.price_publication_time)
    return TimedSource(name, data, rule), snaps


def load_weather(
    raw_root: Path, loc: Location, kind: WeatherKind, cfg: DatasetConfig, variables: list[str]
) -> tuple[TimedSource, list[Snapshot]]:
    """One location's weather under a canonical schema (suffix stripped, hours aligned)."""
    wcfg = cfg.weather
    src = wcfg.sources[kind]
    dataset = f"{src.endpoint}__{wcfg.model}"
    raw, snaps = load_latest(raw_root, "open_meteo", dataset, loc.name)
    if raw.empty:
        raise FileNotFoundError(f"no valid {dataset} snapshots for {loc.name}")
    cols = {f"{v}{src.column_suffix}": v for v in variables}
    missing = [c for c in cols if c not in raw.columns]
    if missing:
        raise KeyError(f"{dataset}/{loc.name} lacks columns {missing}")
    frame = raw.set_index("timestamp")[list(cols)].rename(columns=cols).astype("float64")
    if src.live_endpoint:
        # The same runs fetched directly (identical values where both exist; see
        # ingest/openmeteo.py). They cover hours the archive does not hold yet.
        live, live_snaps = load_latest(
            raw_root, "open_meteo", f"{src.live_endpoint}__{wcfg.model}", loc.name
        )
        if not live.empty:
            live_frame = live.set_index("timestamp")[list(cols)].rename(columns=cols)
            frame = live_frame.astype("float64").combine_first(frame)
            snaps = [*snaps, *live_snaps]
    # Preceding-hour variables: the value labelled T covers (T-1h, T]; relabel to T-1h so a
    # row describes the hour that starts at its timestamp, like prices.
    shifted = [v for v in variables if v in wcfg.preceding_hour_variables]
    if shifted:
        moved = frame[shifted].copy()
        moved.index = pd.DatetimeIndex(moved.index) - pd.Timedelta(hours=1)
        frame = frame.drop(columns=shifted).join(moved, how="outer")[variables]
    frame.index.name = "valid_time"
    note = (
        "stitched historical forecast; true issue time unknown, treated as this lead (optimistic)"
        if kind == "stitched"
        else "Open-Meteo Previous Runs fixed lead"
    )
    rule = fixed_lead_rule(src.lead_days * 24, wcfg.publication_delay_hours, note)
    return TimedSource(f"wx_{loc.name}", frame.sort_index(), rule), snaps


def weather_source_tag(kind: WeatherKind, cfg: DatasetConfig) -> str:
    src = cfg.weather.sources[kind]
    return f"open-meteo:{src.endpoint}:{cfg.weather.model}:lead{src.lead_days}d"
