from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from pricefc.backtest.harness import run_backtest
from pricefc.backtest.metrics import qcol
from pricefc.models import ensemble as ens
from pricefc.models.ensemble import EnsembleForecaster, best_weights, simplex_grid

QS = [0.1, 0.5, 0.9]
TAUS = np.array(QS)


def frame(days: int = 40, zone: str = "SE3") -> pd.DataFrame:
    """One origin per day, 24 target hours; `oracle` = y, visible to fake members only."""
    rows = []
    rng = np.random.default_rng(0)
    for d in range(days):
        od = date(2025, 1, 1) + timedelta(days=d)
        origin = pd.Timestamp(od, tz="UTC") + pd.Timedelta(hours=8)
        for h in range(24):
            y = 50 + 20 * np.sin(h / 24 * 2 * np.pi) + rng.normal(0, 5)
            rows.append(
                {
                    "zone": zone,
                    "origin": origin,
                    "origin_date": od.isoformat(),
                    "target_time": pd.Timestamp(od + timedelta(days=1), tz="UTC")
                    + pd.Timedelta(hours=h),
                    "target_date": (od + timedelta(days=1)).isoformat(),
                    "cal_hour": h,
                    "oracle": y,
                    "y": y,
                    "y_n_periods": 1.0,
                    "y_is_pt15m": 0.0,
                }
            )
    return pd.DataFrame(rows)


class Fake:
    """Forecasts oracle + bias(hour) with a fixed fan; counts predict calls."""

    family = "test"

    def __init__(self, bias: Any, refit: str = "monthly", tags: dict[str, str] | None = None):
        self.bias, self.refit, self.name = bias, refit, "fake"
        self.calls = 0
        self.tags = tags or {}

    def fit(self, train: pd.DataFrame) -> None:
        assert "y" in train  # training rows carry the target

    def predict(self, origin: pd.Timestamp, features: pd.DataFrame) -> pd.DataFrame:
        assert "y" not in features
        self.calls += 1
        b = np.array([self.bias(h) for h in features["cal_hour"]])
        mid = features["oracle"].to_numpy() + b
        return pd.DataFrame(
            {qcol(0.1): mid - 5, qcol(0.5): mid, qcol(0.9): mid + 5}, index=features.index
        )

    def params(self) -> dict[str, Any]:
        return {}

    def version_info(self) -> dict[str, Any]:
        return {}

    def run_tags(self) -> dict[str, str]:
        return self.tags


def ensemble(members: list[Fake], **kw: Any) -> EnsembleForecaster:
    specs = [f"m{i}" for i in range(len(members))]
    return EnsembleForecaster(members, specs, QS, **kw)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def clear_memo() -> None:
    ens._MEMO.clear()


def test_simplex_grid() -> None:
    g2, g3 = simplex_grid(2, 0.05), simplex_grid(3, 0.05)
    assert g2.shape == (21, 2) and g3.shape == (231, 3)
    assert np.allclose(g2.sum(axis=1), 1) and np.allclose(g3.sum(axis=1), 1)


def test_best_weights_pick_the_accurate_member() -> None:
    y = np.linspace(0, 100, 200)
    good = np.stack([y - 5, y, y + 5], axis=1)
    bad = good + 30
    w = best_weights(np.stack([bad, good]), y, TAUS, simplex_grid(2, 0.05))
    assert w.tolist() == [0.0, 1.0]


def test_hourly_weights_follow_the_member_that_is_better_at_each_hour() -> None:
    night = Fake(lambda h: 0.0 if h < 6 else 25.0)
    day = Fake(lambda h: 25.0 if h < 6 else 0.0)
    df = frame()
    origins = [date.fromisoformat(d) for d in sorted(df["origin_date"].unique())][1:]
    e = ensemble([night, day], weighting="hourly", min_days=7)
    res = run_backtest(e, df, df, origins, QS)  # type: ignore[arg-type]
    assert e._weights[2].tolist() == [1.0, 0.0] and e._weights[14].tolist() == [0.0, 1.0]
    late = res.forecasts[res.forecasts["target_date"] > "2025-01-20"]
    assert np.allclose(late[qcol(0.5)], late["y"], atol=1e-9)  # both regimes picked perfectly


def test_equal_weights_until_min_days_and_weights_use_only_published_days() -> None:
    a, b = Fake(lambda h: 0.0), Fake(lambda h: 10.0)
    df = frame(30)
    origins = [date.fromisoformat(d) for d in sorted(df["origin_date"].unique())][1:]
    e = ensemble([a, b], weighting="global", min_days=10)
    res = run_backtest(e, df, df, origins, QS)  # type: ignore[arg-type]
    first = res.forecasts[res.forecasts["origin_date"] == origins[0].isoformat()]
    assert np.allclose(first[qcol(0.5)], first["y"] + 5)  # 50/50 during the cold start
    for entry in e.weight_log:
        assert entry["days"] >= 10
    # weights after origin D are estimated from target days <= D: the first log entry comes
    # once 10 forecast target days are published, i.e. at origin first_target + 9 days
    first_target = date.fromisoformat(first["target_date"].iloc[0])
    assert e.weight_log[0]["last_target_date"] == (first_target + timedelta(days=9)).isoformat()


def test_members_refit_on_their_own_cadence() -> None:
    fits: list[str] = []

    class Counting(Fake):
        def fit(self, train: pd.DataFrame) -> None:
            fits.append(str(train["target_date"].max()))

    df = frame(40)
    origins = [date.fromisoformat(d) for d in sorted(df["origin_date"].unique())][1:]
    e = ensemble([Counting(lambda h: 0.0), Fake(lambda h: 1.0)], weighting="equal")
    run_backtest(e, df, df, origins, QS)  # type: ignore[arg-type]
    assert len(fits) == 2  # monthly member: once in January, once in February


def test_member_forecasts_are_reused_across_ensemble_variants() -> None:
    a, b = Fake(lambda h: 0.0), Fake(lambda h: 3.0)
    df = frame(10)
    origins = [date.fromisoformat(d) for d in sorted(df["origin_date"].unique())][1:]
    run_backtest(ensemble([a, b], weighting="equal"), df, df, origins, QS)  # type: ignore[arg-type]
    calls = a.calls
    run_backtest(ensemble([a, b], weighting="hourly"), df, df, origins, QS)  # type: ignore[arg-type]
    assert a.calls == calls == len(origins)


def test_deployable_only_if_every_member_is() -> None:
    ok = Fake(lambda h: 0.0, tags={"deployable": "true", "model_licence": "apache-2.0"})
    nc = Fake(lambda h: 0.0, tags={"deployable": "false", "model_licence": "non-commercial"})
    assert ensemble([ok, Fake(lambda h: 0.0)]).run_tags()["deployable"] == "true"
    tags = ensemble([ok, nc]).run_tags()
    assert tags["deployable"] == "false" and tags["model_licence"] == "apache-2.0,non-commercial"
