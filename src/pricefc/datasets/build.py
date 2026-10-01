"""Build one dataset: rows = (origin day D, target hour of D+1), features + target y.

The builder fails if the leakage audit finds any feature that changes when only data
unavailable at the origin is perturbed (spec section 6.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import structlog

from pricefc.config import BaseConfig, FeaturesConfig, IngestConfig
from pricefc.datasets import features as F
from pricefc.datasets.loaders import (
    WeatherKind,
    load_hourly_prices,
    load_weather,
    weather_source_tag,
)
from pricefc.datasets.sources import TimedSource
from pricefc.ingest.snapshot import Snapshot
from pricefc.timeutils import local_day_bounds_utc

log = structlog.get_logger(__name__)

META_COLUMNS = (
    "zone",
    "origin",
    "origin_date",
    "target_time",
    "target_date",
    "y_n_periods",
    "y_is_pt15m",
)
TARGET = "y"


class LeakageError(RuntimeError):
    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__(f"leakage detected in features: {report['leaky_features'][:10]}")
        self.report = report


def origin_timestamp(day: date, base: BaseConfig) -> pd.Timestamp:
    t = base.origin.local_time
    local = pd.Timestamp(datetime.combine(day, t))
    return local.tz_localize(base.timezone).tz_convert("UTC")


def target_hours(day: date, tz: str) -> pd.DatetimeIndex:
    """UTC start of every hour in local day `day` (23, 24 or 25 hours)."""
    start, end = local_day_bounds_utc(day, tz)
    return pd.date_range(start, end, freq="h", inclusive="left", name="target_time")


@dataclass
class DatasetBuilder:
    base: BaseConfig
    features: FeaturesConfig
    zone: str
    weather_kind: WeatherKind
    sources: dict[str, TimedSource]
    groups: list[F.FeatureGroup]
    lineage: dict[str, list[Snapshot]] = field(default_factory=dict)

    # --- construction --------------------------------------------------------------------------

    @classmethod
    def from_snapshots(
        cls,
        base: BaseConfig,
        features: FeaturesConfig,
        ingest: IngestConfig,
        zone: str,
        weather_kind: WeatherKind,
    ) -> DatasetBuilder:
        cfg = features.dataset
        raw = base.paths.raw
        price_source = ingest.prices.source
        sources: dict[str, TimedSource] = {}
        lineage: dict[str, list[Snapshot]] = {}
        sources[F.PRICE], lineage[F.PRICE] = load_hourly_prices(
            raw, price_source, zone, base, cfg, F.PRICE
        )
        for nb in cfg.neighbour_price_zones.get(zone, []):  # type: ignore[call-overload]
            key = f"{F.PRICE}_{nb}"
            sources[key], lineage[key] = load_hourly_prices(raw, price_source, nb, base, cfg, key)
        locations = features.weather.locations[zone]  # type: ignore[index]
        variables = ingest.open_meteo.variables
        for loc in locations:
            key = f"wx_{loc.name}"
            sources[key], lineage[key] = load_weather(raw, loc, weather_kind, cfg, variables)
        groups = default_groups(
            features,
            zone,
            [loc.name for loc in locations],
            variables,
        )
        return cls(base, features, zone, weather_kind, sources, groups, lineage)

    # --- building --------------------------------------------------------------------------------

    def build_origin(
        self, day: date, sources: Mapping[str, TimedSource] | None = None
    ) -> pd.DataFrame:
        srcs = sources or self.sources
        targets = target_hours(day + timedelta(days=1), self.base.timezone)
        ctx = F.OriginContext(
            zone=self.zone,
            origin_day=day,
            origin=origin_timestamp(day, self.base),
            targets=targets,
            tz=self.base.timezone,
            sources=srcs,
        )
        parts = [g.build(ctx) for g in self.groups]
        feats = pd.concat(parts, axis=1)
        dup = feats.columns[feats.columns.duplicated()].tolist()
        if dup:
            raise ValueError(f"duplicate feature columns: {dup}")
        price = srcs[F.PRICE].data.reindex(targets)  # the target: never a feature input
        meta = pd.DataFrame(
            {
                "zone": self.zone,
                "origin": ctx.origin,
                "origin_date": day.isoformat(),
                "target_time": targets,
                "target_date": (day + timedelta(days=1)).isoformat(),
                TARGET: price["price"].to_numpy(),
                "y_n_periods": price["n_periods"].to_numpy(),
                "y_is_pt15m": price["is_pt15m"].to_numpy(),
            },
            index=targets,
        )
        out = pd.concat([meta, feats], axis=1).reset_index(drop=True)
        # Fixed timestamp unit so the content digest (and dataset version) does not depend on
        # how pandas happened to infer units.
        for col in ("origin", "target_time"):
            out[col] = out[col].dt.as_unit("ns")
        return out

    def origin_range(self) -> tuple[date, date]:
        """Widest origin range the loaded data supports.

        Origin D needs: prices back to the longest lag day (D+1 - max_lag), neighbour prices
        for D, weather for every hour of D (24h changes) and D+1, and the target day's prices.
        Coverage is checked in UTC against each local day's bounds.
        """
        tz = self.base.timezone
        cfg = self.features.dataset
        max_lag = max(*cfg.price_lag_days, cfg.same_hour_mean_days)

        def span(name: str) -> tuple[pd.Timestamp, pd.Timestamp]:
            idx = self.sources[name].data.dropna(how="all").index
            return idx.min(), idx.max() + pd.Timedelta(hours=1)  # [first, end)

        def covers(name: str, first_day: date, last_day: date) -> bool:
            lo, hi = span(name)
            return lo <= local_day_bounds_utc(first_day, tz)[0] and (
                local_day_bounds_utc(last_day, tz)[1] <= hi
            )

        lo, hi = span(F.PRICE)
        day, last_day = lo.tz_convert(tz).date(), hi.tz_convert(tz).date()
        candidates = []
        while day <= last_day:
            ok = covers(F.PRICE, day + timedelta(days=1 - max_lag), day + timedelta(days=1))
            ok = ok and all(
                covers(n, day, day)
                if n.startswith(f"{F.PRICE}_")
                else covers(n, day, day + timedelta(days=1))
                for n in self.sources
                if n != F.PRICE
            )
            if ok:
                candidates.append(day)
            day += timedelta(days=1)
        if not candidates:
            raise ValueError("no usable origins in the loaded data")
        return candidates[0], candidates[-1]

    def build(self, first: date, last: date) -> tuple[pd.DataFrame, dict[str, Any]]:
        """All origins first..last. Rows without a target (e.g. excluded source days) are
        dropped and reported."""
        frames = []
        day = first
        while day <= last:
            frames.append(self.build_origin(day))
            day += timedelta(days=1)
        df = pd.concat(frames, ignore_index=True)
        no_target = df[TARGET].isna()
        info = {
            "origins": (last - first).days + 1,
            "rows_built": len(df),
            "rows_dropped_no_target": int(no_target.sum()),
            "target_dates_dropped": sorted(df.loc[no_target, "target_date"].unique().tolist()),
        }
        df = df[~no_target].reset_index(drop=True)
        return df, info

    # --- leakage audit ---------------------------------------------------------------------------

    def feature_columns(self, df: pd.DataFrame) -> list[str]:
        return [c for c in df.columns if c not in (*META_COLUMNS, TARGET)]

    def leakage_audit(self, origins: list[date]) -> dict[str, Any]:
        """Perturb all data unavailable at each origin; every feature must stay identical.

        Also asserts that the perturbation reached the target (the D+1 prices), which shows
        the check is live rather than vacuous.
        """
        rng = np.random.default_rng(self.features.dataset.leakage_audit.seed)
        leaky: set[str] = set()
        checked: list[str] = []
        target_changed = 0
        for day in origins:
            t0 = origin_timestamp(day, self.base)
            clean = self.build_origin(day)
            perturbed_sources = {n: s.perturbed_after(t0, rng) for n, s in self.sources.items()}
            dirty = self.build_origin(day, perturbed_sources)
            cols = self.feature_columns(clean)
            a = clean[cols].to_numpy(dtype="float64")
            b = dirty[cols].to_numpy(dtype="float64")
            same = (a == b) | (np.isnan(a) & np.isnan(b))
            leaky.update(c for c, ok in zip(cols, same.all(axis=0), strict=True) if not ok)
            y_a, y_b = clean[TARGET].to_numpy(), dirty[TARGET].to_numpy()
            target_changed += int(not np.array_equal(y_a, y_b, equal_nan=True))
            checked.append(day.isoformat())
        report = {
            "origins_checked": checked,
            "n_features": len(cols),
            "leaky_features": sorted(leaky),
            "perturbation_reached_target": target_changed,
            "passed": not leaky and target_changed == len(origins),
        }
        if leaky:
            raise LeakageError(report)
        if target_changed != len(origins):
            raise RuntimeError(f"leakage audit is vacuous: target unchanged ({report})")
        return report

    def audit_origins(self, first: date, last: date) -> list[date]:
        """Seeded random sample plus both ends and every origin whose target day has 23 or
        25 hours (DST changes are where time arithmetic usually goes wrong)."""
        cfg = self.features.dataset.leakage_audit
        rng = np.random.default_rng(cfg.seed)
        all_days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        picks = rng.choice(len(all_days), min(cfg.n_origins, len(all_days)), replace=False)
        days = {all_days[0], all_days[-1], *(all_days[int(i)] for i in picks)}
        tz = self.base.timezone
        days.update(d for d in all_days if len(target_hours(d + timedelta(days=1), tz)) != 24)
        return sorted(days)

    def describe(self) -> dict[str, Any]:
        return {
            "zone": self.zone,
            "weather_kind": self.weather_kind,
            "weather_source": weather_source_tag(self.weather_kind, self.features.dataset),
            "groups": [{"name": g.name, "sources": list(g.sources)} for g in self.groups],
            "sources": {
                n: {
                    "rule": s.rule.describe(),
                    "rows": len(s.data),
                    "columns": list(s.data.columns),
                    "snapshots": [
                        {
                            "path": str(snap.path),
                            "data_sha256": snap.manifest["data_sha256"],
                            "git_sha": snap.manifest["git_sha"],
                        }
                        for snap in self.lineage.get(n, [])
                    ],
                }
                for n, s in self.sources.items()
            },
        }


def default_groups(
    features: FeaturesConfig, zone: str, locations: list[str], variables: list[str]
) -> list[F.FeatureGroup]:
    cfg = features.dataset
    groups = [
        F.FeatureGroup("calendar", (), F.calendar_features),
        F.FeatureGroup("price", (F.PRICE,), F.price_features(cfg)),
    ]
    for nb in cfg.neighbour_price_zones.get(zone, []):  # type: ignore[call-overload]
        groups.append(
            F.FeatureGroup(
                f"neighbour_{nb}", (F.PRICE, f"{F.PRICE}_{nb}"), F.neighbour_price_features(nb)
            )
        )
    groups.append(
        F.FeatureGroup(
            "weather",
            tuple(f"wx_{loc}" for loc in locations),
            F.weather_features(locations, variables, cfg.weather.direction_variables),
        )
    )
    return groups


def build_dataset(
    base: BaseConfig,
    features: FeaturesConfig,
    ingest: IngestConfig,
    zone: str,
    weather_kind: WeatherKind,
    first: date | None = None,
    last: date | None = None,
    *,
    log_to_mlflow: bool = True,
) -> tuple[Any, str | None]:
    """Load sources, build all origins, run the leakage audit, store and log the dataset."""
    from pricefc.datasets.registry import log_dataset_run, write_dataset

    builder = DatasetBuilder.from_snapshots(base, features, ingest, zone, weather_kind)
    lo, hi = builder.origin_range()
    first, last = max(first or lo, lo), min(last or hi, hi)
    log.info("building", zone=zone, weather=weather_kind, first=str(first), last=str(last))
    df, info = builder.build(first, last)
    report = builder.leakage_audit(builder.audit_origins(first, last))
    info["meta_columns"] = list(META_COLUMNS)
    desc = builder.describe()
    desc["price_source"] = ingest.prices.source
    stored = write_dataset(
        df,
        base=base,
        features=features,
        zone=zone,
        name=f"{zone.lower()}-{base.resolution}-{weather_kind}",
        description=desc,
        build_info=info,
        leakage_report=report,
    )
    run_id = log_dataset_run(base, stored, df) if log_to_mlflow else None
    return stored, run_id
