"""Shared web dashboard and metrics constants."""

BACKTEST_BASELINES: dict[str, dict[str, float]] = {
    "SE1": {"pinball": 4.61, "naive_7d_pinball": 11.12},
    "SE2": {"pinball": 4.53, "naive_7d_pinball": 11.48},
    "SE3": {"pinball": 5.25, "naive_7d_pinball": 11.61},
    "SE4": {"pinball": 6.39, "naive_7d_pinball": 13.69},
}
