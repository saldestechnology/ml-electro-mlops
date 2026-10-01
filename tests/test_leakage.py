"""Leakage rules (spec 6.2, 13): features may only depend on data available at the origin."""

from datetime import date, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st

from pricefc.config import load_config, load_features_config, load_ingest_config
from pricefc.datasets import features as F
from pricefc.datasets.build import DatasetBuilder, LeakageError, default_groups, origin_timestamp
from pricefc.datasets.sources import TimedSource, day_ahead_price_rule, fixed_lead_rule

BASE = load_config(Path("configs/base.yaml"))
FEATS = load_features_config(Path("configs/features.yaml"))
VARIABLES = load_ingest_config(Path("configs/ingest.yaml")).open_meteo.variables
TZ = BASE.timezone
# Spans the spring DST change (2025-03-30) so 23-hour target days are exercised.
START, END = pd.Timestamp("2025-03-01", tz="UTC"), pd.Timestamp("2025-04-20", tz="UTC")
FIRST_ORIGIN, LAST_ORIGIN = date(2025, 3, 10), date(2025, 4, 17)


def synthetic_sources(seed: int) -> dict[str, TimedSource]:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(START, END, freq="h", inclusive="left", name="valid_time")
    price_rule = day_ahead_price_rule(TZ, FEATS.dataset.price_publication_time)
    wx_rule = fixed_lead_rule(48, FEATS.dataset.weather.publication_delay_hours)

    def price() -> pd.DataFrame:
        p = 50 + np.cumsum(rng.normal(0, 5, len(idx)))
        return pd.DataFrame({"price": p, "n_periods": 1.0, "is_pt15m": 0.0}, index=idx)

    def weather() -> pd.DataFrame:
        cols = {v: rng.uniform(0, 300, len(idx)) for v in VARIABLES}
        return pd.DataFrame(cols, index=idx)

    sources = {F.PRICE: TimedSource(F.PRICE, price(), price_rule)}
    for nb in FEATS.dataset.neighbour_price_zones["SE3"]:
        sources[f"price_{nb}"] = TimedSource(f"price_{nb}", price(), price_rule)
    for loc in ("a", "b"):
        sources[f"wx_{loc}"] = TimedSource(f"wx_{loc}", weather(), wx_rule)
    return sources


def builder(seed: int = 0) -> DatasetBuilder:
    groups = default_groups(FEATS, "SE3", ["a", "b"], VARIABLES)
    return DatasetBuilder(BASE, FEATS, "SE3", "true_lead", synthetic_sources(seed), groups)


# --- availability rules: known answers ----------------------------------------------------------


def test_price_for_d_plus_1_is_hidden_at_origin_but_d_is_visible() -> None:
    rule = day_ahead_price_rule(TZ, time(13, 0))
    origin = origin_timestamp(date(2025, 6, 10), BASE)  # 09:00 local on D
    d = pd.Timestamp("2025-06-10 23:00", tz=TZ).tz_convert("UTC")  # last hour of D
    d1 = pd.Timestamp("2025-06-11 00:00", tz=TZ).tz_convert("UTC")  # first hour of D+1
    avail = rule.available_at(pd.DatetimeIndex([d, d1]))
    assert avail[0] <= origin < avail[1]
    assert avail[1] == pd.Timestamp("2025-06-10 13:00", tz=TZ)


def test_weather_lead2_visible_for_all_targets_but_lead1_is_not() -> None:
    delay = FEATS.dataset.weather.publication_delay_hours
    origin = origin_timestamp(date(2025, 6, 10), BASE)
    targets = pd.date_range(pd.Timestamp("2025-06-11", tz=TZ), periods=24, freq="h").tz_convert(
        "UTC"
    )
    lead2 = fixed_lead_rule(48, delay).available_at(targets)
    lead1 = fixed_lead_rule(24, delay).available_at(targets)
    assert (lead2 <= origin).all()
    assert (lead1 > origin).sum() > 12  # day-1 lead would leak for most target hours


# --- the invariant ------------------------------------------------------------------------------


@given(
    seed=st.integers(0, 2**16),
    offset=st.integers(0, (LAST_ORIGIN - FIRST_ORIGIN).days),
    frac=st.floats(0.05, 1.0),
    scale=st.sampled_from([1e-6, 1.0, 1e3, 1e9]),
    nan_frac=st.floats(0.0, 1.0),
)
def test_features_ignore_any_change_to_unavailable_data(
    seed: int, offset: int, frac: float, scale: float, nan_frac: float
) -> None:
    b = builder(seed % 7)
    day = FIRST_ORIGIN + timedelta(days=offset)
    origin = origin_timestamp(day, BASE)
    rng = np.random.default_rng(seed)
    changed: dict[str, TimedSource] = {}
    for name, src in b.sources.items():
        data = src.data.copy()
        late = (src.available_at > origin).to_numpy()
        pick = late & (rng.random(len(data)) < frac)
        for col in data.columns:
            vals = rng.normal(0, scale, int(pick.sum()))
            vals[rng.random(len(vals)) < nan_frac] = np.nan
            data.loc[pick, col] = vals
        changed[name] = TimedSource(name, data, src.rule)

    clean = b.build_origin(day)
    dirty = b.build_origin(day, changed)
    cols = b.feature_columns(clean)
    pd.testing.assert_frame_equal(clean[cols], dirty[cols], check_exact=True)


# --- the audit ----------------------------------------------------------------------------------


def test_audit_passes_and_is_not_vacuous() -> None:
    b = builder()
    report = b.leakage_audit([date(2025, 3, 29), date(2025, 4, 5)])  # incl. a 23-hour D+1
    assert report["passed"] and report["perturbation_reached_target"] == 2


def test_injected_post_origin_feature_is_caught() -> None:
    def peek(ctx: F.OriginContext) -> pd.DataFrame:
        # Bug on purpose: reads the raw source instead of ctx.view(), i.e. D+1 prices.
        raw = ctx.sources[F.PRICE].data["price"].reindex(ctx.targets)
        return pd.DataFrame({"leaky_tomorrow_price": raw.to_numpy()}, index=ctx.targets)

    b = builder()
    b.groups.append(F.FeatureGroup("leaky", (F.PRICE,), peek))
    with pytest.raises(LeakageError) as err:
        b.leakage_audit([date(2025, 4, 5)])
    assert err.value.report["leaky_features"] == ["leaky_tomorrow_price"]


def test_subtle_leak_via_lag_zero_is_caught() -> None:
    """A 'same hour, 0 days back' lag through the raw source is tomorrow's price."""

    def lag0(ctx: F.OriginContext) -> pd.DataFrame:
        at = F.same_local_hour(ctx.targets, ctx.tz, 0)
        vals = F.lookup(ctx.sources[F.PRICE].data, "price", at)
        return pd.DataFrame({"p_lag0d": vals}, index=ctx.targets)

    b = builder()
    b.groups.append(F.FeatureGroup("lag0", (F.PRICE,), lag0))
    with pytest.raises(LeakageError):
        b.leakage_audit([date(2025, 4, 5)])
