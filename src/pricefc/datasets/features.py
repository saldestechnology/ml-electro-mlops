"""Feature groups for one forecast origin (spec section 6.1).

An origin is local day D at the configured origin time; targets are the hours of local day
D+1 (23, 24 or 25 of them). Each feature group reads its inputs only through
`ctx.view(source_name)`, which returns values available at the origin. The leakage audit
(`build.leakage_audit`) verifies this empirically.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cache

import numpy as np
import pandas as pd
from holidays.countries.sweden import Sweden

from pricefc.config import DatasetConfig
from pricefc.datasets.sources import TimedSource

PRICE = "price"


@dataclass
class OriginContext:
    zone: str
    origin_day: date
    origin: pd.Timestamp
    targets: pd.DatetimeIndex
    tz: str
    sources: Mapping[str, TimedSource]
    _views: dict[str, pd.DataFrame] = field(default_factory=dict)

    def view(self, name: str) -> pd.DataFrame:
        """Values of `name` known at the origin (the only access feature groups should use)."""
        if name not in self._views:
            self._views[name] = self.sources[name].as_of(self.origin)
        return self._views[name]


@dataclass(frozen=True)
class FeatureGroup:
    name: str
    sources: tuple[str, ...]  # declared inputs; recorded in the dataset manifest
    build: Callable[[OriginContext], pd.DataFrame]  # index: ctx.targets


# --- helpers -------------------------------------------------------------------------------


def same_local_hour(targets: pd.DatetimeIndex, tz: str, days_back: int) -> pd.DatetimeIndex:
    """UTC timestamps of the same local clock hour `days_back` days earlier.

    Hours that do not exist on the earlier day (spring DST) map to NaT; ambiguous autumn
    hours map to their first occurrence.
    """
    local = targets.tz_convert(tz).tz_localize(None) - pd.Timedelta(days=days_back)
    out = local.tz_localize(tz, ambiguous=np.ones(len(local), dtype=bool), nonexistent="NaT")
    return out.tz_convert("UTC")


def lookup(view: pd.DataFrame, col: str, at: pd.DatetimeIndex) -> np.ndarray:
    return view[col].reindex(at.as_unit("ns")).to_numpy(dtype="float64")


def local_day_slice(view: pd.DataFrame, day: date, tz: str) -> pd.DataFrame:
    start = pd.Timestamp(day).tz_localize(tz).tz_convert("UTC")
    end = pd.Timestamp(day + timedelta(days=1)).tz_localize(tz).tz_convert("UTC")
    return view[(view.index >= start) & (view.index < end)]


# --- calendar --------------------------------------------------------------------------------


# Holiday names are localised from the system locale; the name matching below needs a fixed
# language, or the calendar features would differ between machines.
_LANG = "en_US"


@cache
def _sweden_days(year: int) -> tuple[frozenset[date], frozenset[date], frozenset[date]]:
    """(public, de facto, half days) for one year. Plain Sundays are excluded."""
    public = Sweden(years=year, categories=("public",), language=_LANG)
    de_facto = Sweden(years=year, categories=("de_facto",), language=_LANG)
    every = Sweden(years=year, categories=Sweden.supported_categories, language=_LANG)
    pub = frozenset(d for d, n in public.items() if n != "Sunday")
    half = frozenset(d for d, n in every.items() if "(from 2pm)" in n)
    return pub, frozenset(de_facto.keys()), half


def _day_flags(d: date) -> tuple[bool, bool, bool]:
    pub, de_facto, half = _sweden_days(d.year)
    return d in pub, d in de_facto, d in half


def _is_off(d: date) -> bool:
    pub, de_facto, _ = _day_flags(d)
    return d.weekday() >= 5 or pub or de_facto


def _is_bridge(d: date) -> bool:
    """Working weekday squeezed between a holiday and an off day (klämdag)."""
    if _is_off(d):
        return False
    prev_, next_ = d - timedelta(days=1), d + timedelta(days=1)
    return (_is_holiday(prev_) and _is_off(next_)) or (_is_holiday(next_) and _is_off(prev_))


def _is_holiday(d: date) -> bool:
    pub, de_facto, _ = _day_flags(d)
    return pub or de_facto


def _days_to_holiday(d: date, step: int, cap: int = 14) -> int:
    for k in range(1, cap + 1):
        x = d + timedelta(days=step * k)
        if any(_day_flags(x)[:2]):
            return k
    return cap


def calendar_features(ctx: OriginContext) -> pd.DataFrame:
    local = ctx.targets.tz_convert(ctx.tz)
    day = ctx.origin_day + timedelta(days=1)
    pub, de_facto, half = _day_flags(day)
    hour = local.hour.to_numpy()
    doy = local.dayofyear.to_numpy()
    out = pd.DataFrame(
        {
            "cal_hour": hour,
            "cal_hour_sin": np.sin(2 * np.pi * hour / 24),
            "cal_hour_cos": np.cos(2 * np.pi * hour / 24),
            "cal_dow": day.weekday(),
            "cal_is_weekend": day.weekday() >= 5,
            "cal_month": day.month,
            "cal_doy_sin": np.sin(2 * np.pi * doy / 365.25),
            "cal_doy_cos": np.cos(2 * np.pi * doy / 365.25),
            "cal_is_public_holiday": pub,
            "cal_is_de_facto_holiday": de_facto,
            "cal_is_half_day": half,
            "cal_is_bridge_day": _is_bridge(day),
            "cal_days_to_holiday": _days_to_holiday(day, +1),
            "cal_days_since_holiday": _days_to_holiday(day, -1),
            "cal_hours_in_day": len(ctx.targets),
            "cal_is_dst": [bool(t.dst()) for t in local],
            "lead_hours": ((ctx.targets - ctx.origin) / pd.Timedelta(hours=1)).to_numpy(),
        },
        index=ctx.targets,
    )
    bool_cols = [c for c in out.columns if c.startswith("cal_is_")]
    return out.astype({c: "int8" for c in bool_cols})


# --- prices ----------------------------------------------------------------------------------


def price_features(cfg: DatasetConfig) -> Callable[[OriginContext], pd.DataFrame]:
    def build(ctx: OriginContext) -> pd.DataFrame:
        view = ctx.view(PRICE)
        out: dict[str, np.ndarray | float] = {}
        n_same = max(cfg.same_hour_mean_days, *cfg.price_lag_days)
        lags = {
            k: lookup(view, "price", same_local_hour(ctx.targets, ctx.tz, k))
            for k in range(1, n_same + 1)
        }
        for k in cfg.price_lag_days:
            out[f"p_lag{k}d"] = lags[k]
        stacked = np.vstack([lags[k] for k in range(1, cfg.same_hour_mean_days + 1)])
        with np.errstate(all="ignore"):
            out[f"p_samehour_mean{cfg.same_hour_mean_days}d"] = np.nanmean(stacked, axis=0)

        prices = view["price"].dropna()
        last = prices.index.max() if len(prices) else None
        for w in cfg.rolling_windows_hours:
            win = prices[prices.index > last - pd.Timedelta(hours=w)] if last else prices
            out[f"p_roll{w}h_mean"] = float(win.mean()) if len(win) else np.nan
            out[f"p_roll{w}h_min"] = float(win.min()) if len(win) else np.nan
            out[f"p_roll{w}h_max"] = float(win.max()) if len(win) else np.nan
            out[f"p_roll{w}h_std"] = float(win.std()) if len(win) > 1 else np.nan
        d0 = local_day_slice(view, ctx.origin_day, ctx.tz)["price"]
        d1 = local_day_slice(view, ctx.origin_day - timedelta(days=1), ctx.tz)["price"]
        out["p_dayD_mean"] = float(d0.mean()) if len(d0) else np.nan
        out["p_dayD_trend"] = out["p_dayD_mean"] - (float(d1.mean()) if len(d1) else np.nan)
        out["p_dayD_neg_hours"] = float((d0 < 0).sum()) if len(d0) else np.nan
        return pd.DataFrame(out, index=ctx.targets)

    return build


def neighbour_price_features(neighbour: str) -> Callable[[OriginContext], pd.DataFrame]:
    src = f"{PRICE}_{neighbour}"

    def build(ctx: OriginContext) -> pd.DataFrame:
        view = ctx.view(src)
        own = ctx.view(PRICE)
        at = same_local_hour(ctx.targets, ctx.tz, 1)
        nb_lag1 = lookup(view, "price", at)
        d0 = local_day_slice(view, ctx.origin_day, ctx.tz)["price"]
        return pd.DataFrame(
            {
                f"nb_{neighbour}_lag1d": nb_lag1,
                f"nb_{neighbour}_dayD_mean": float(d0.mean()) if len(d0) else np.nan,
                f"nb_{neighbour}_spread_lag1d": lookup(own, "price", at) - nb_lag1,
            },
            index=ctx.targets,
        )

    return build


# --- weather ---------------------------------------------------------------------------------


def weather_features(
    locations: list[str], variables: list[str], direction_vars: list[str]
) -> Callable[[OriginContext], pd.DataFrame]:
    scalar_vars = [v for v in variables if v not in direction_vars]

    def build(ctx: OriginContext) -> pd.DataFrame:
        out: dict[str, np.ndarray] = {}
        per_var: dict[str, list[np.ndarray]] = {v: [] for v in scalar_vars}
        prev_temp: list[np.ndarray] = []
        prev_at = ctx.targets - pd.Timedelta(hours=24)
        for loc in locations:
            view = ctx.view(f"wx_{loc}")
            for v in scalar_vars:
                vals = lookup(view, v, ctx.targets)
                out[f"w_{loc}_{v}"] = vals
                per_var[v].append(vals)
            for v in direction_vars:
                rad = np.deg2rad(lookup(view, v, ctx.targets))
                out[f"w_{loc}_{v}_sin"] = np.sin(rad)
                out[f"w_{loc}_{v}_cos"] = np.cos(rad)
            if "temperature_2m" in scalar_vars:
                prev_temp.append(lookup(view, "temperature_2m", prev_at))
        with np.errstate(all="ignore"), warnings.catch_warnings():
            # All-NaN hours (weather archive gaps) are expected; they stay NaN.
            warnings.simplefilter("ignore", RuntimeWarning)
            for v, arrs in per_var.items():
                stack = np.vstack(arrs)
                out[f"wz_{v}_mean"] = np.nanmean(stack, axis=0)
                out[f"wz_{v}_min"] = np.nanmin(stack, axis=0)
                out[f"wz_{v}_max"] = np.nanmax(stack, axis=0)
            if prev_temp:
                prev = np.nanmean(np.vstack(prev_temp), axis=0)
                out["wz_temperature_2m_d24"] = out["wz_temperature_2m_mean"] - prev
                out["wz_temperature_2m_daymean"] = np.full(
                    len(ctx.targets), np.nanmean(out["wz_temperature_2m_mean"])
                )
        loc_cols = [c for c in out if c.startswith("w_")]
        missing = np.mean([np.isnan(out[c]) for c in loc_cols], axis=0)
        out["weather_missing_frac"] = missing
        return pd.DataFrame(out, index=ctx.targets)

    return build
