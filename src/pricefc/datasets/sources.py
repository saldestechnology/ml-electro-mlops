"""Time series with explicit availability, the basis of leakage control (spec section 6.2).

Every input to feature building is a `TimedSource`: values indexed by UTC valid time plus an
`available_at` timestamp per row, computed by a declared rule. Feature code only ever sees
`source.as_of(origin)`, i.e. rows with `available_at <= origin`. The leakage audit perturbs
everything else and checks that features do not change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class AvailabilityRule:
    """How to compute when a value for valid time T became known."""

    kind: str
    description: str
    params: dict[str, object]

    def available_at(self, valid_time: pd.DatetimeIndex) -> pd.DatetimeIndex:
        if self.kind == "day_ahead_auction":
            # Prices for local delivery day X are published on X-1 at `publication_time`.
            tz = str(self.params["tz"])
            pub = self.params["publication_time"]
            assert isinstance(pub, time)
            local_day = valid_time.tz_convert(tz).normalize().tz_localize(None)
            published = (local_day - pd.Timedelta(days=1)) + pd.Timedelta(
                hours=pub.hour, minutes=pub.minute
            )
            return pd.DatetimeIndex(published).tz_localize(tz).tz_convert("UTC")
        if self.kind == "fixed_lead":
            # Forecast issued `lead_hours` before valid time, published `delay_hours` later.
            lead = float(self.params["lead_hours"])  # type: ignore[arg-type]
            delay = float(self.params["delay_hours"])  # type: ignore[arg-type]
            return valid_time - pd.Timedelta(hours=lead - delay)
        raise ValueError(f"unknown availability rule {self.kind!r}")

    def describe(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "description": self.description,
            "params": {k: str(v) for k, v in self.params.items()},
        }


def day_ahead_price_rule(tz: str, publication_time: time) -> AvailabilityRule:
    return AvailabilityRule(
        "day_ahead_auction",
        f"price for local delivery day X known from X-1 {publication_time:%H:%M} {tz}",
        {"tz": tz, "publication_time": publication_time},
    )


def fixed_lead_rule(lead_hours: float, delay_hours: float, note: str = "") -> AvailabilityRule:
    desc = f"value for T known from T - {lead_hours:g}h + {delay_hours:g}h publication delay"
    return AvailabilityRule(
        "fixed_lead",
        f"{desc}{'; ' + note if note else ''}",
        {"lead_hours": lead_hours, "delay_hours": delay_hours},
    )


class TimedSource:
    """Values indexed by UTC valid time, with per-row availability."""

    def __init__(self, name: str, data: pd.DataFrame, rule: AvailabilityRule) -> None:
        if not isinstance(data.index, pd.DatetimeIndex) or str(data.index.tz) != "UTC":
            raise ValueError(f"{name}: index must be a UTC DatetimeIndex")
        if not data.index.is_monotonic_increasing or not data.index.is_unique:
            raise ValueError(f"{name}: index must be unique and increasing")
        self.name = name
        # One timestamp unit everywhere: mixed units (Parquet gives "us" under pandas 3)
        # push every lookup onto pandas' slow, object-based indexer path.
        data = data.set_axis(data.index.as_unit("ns"))
        self.data = data
        self.rule = rule
        self.available_at = pd.Series(
            rule.available_at(pd.DatetimeIndex(data.index)), index=data.index
        )

    def as_of(self, origin: pd.Timestamp) -> pd.DataFrame:
        """Rows known at `origin`."""
        return self.data[(self.available_at <= origin).to_numpy()]

    def perturbed_after(self, origin: pd.Timestamp, rng: np.random.Generator) -> TimedSource:
        """Copy in which every value not yet available at `origin` is replaced by noise.

        Some rows are set to NaN and some numeric values get large random offsets, so any
        feature that reads them changes.
        """
        data = self.data.copy()
        late = (self.available_at > origin).to_numpy()
        n = int(late.sum())
        if n:
            for col in data.columns:
                if pd.api.types.is_numeric_dtype(data[col]) and not pd.api.types.is_bool_dtype(
                    data[col]
                ):
                    noise = rng.normal(1000.0, 500.0, n)
                    noise[rng.random(n) < 0.1] = np.nan
                    data[col] = data[col].astype("float64")
                    data.loc[late, col] = noise
        return TimedSource(self.name, data, self.rule)
