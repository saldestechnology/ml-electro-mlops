# pricefc — Swedish day-ahead electricity price forecasting

Reproducible, MLflow-versioned pipeline forecasting SE1–SE4 day-ahead prices with quantiles.
Design: see the specification; decisions and deviations are logged in `docs/decisions.md`.

## Setup

```bash
uv sync
cp .env.example .env   # fill in ENTSOE_API_TOKEN etc.
uv run python -m pricefc show-config
uv run python -m pricefc init-tracking
make check             # lint, typecheck, tests
make mlflow-ui         # http://localhost:5000
```

## Datasets

```bash
uv run python -m pricefc dataset build stitched -z SE3    # training: full history
uv run python -m pricefc dataset build true_lead -z SE3   # backtests: true day-2-lead weather
```

Each build runs a leakage audit (fails on any feature that uses data unavailable at the 09:00
origin), writes `data/datasets/{zone}/hourly/{version}/` and logs to MLflow `datasets`.

## Ingestion

```bash
uv run python -m pricefc ingest prices -z SE1 -z SE2 -z SE3 -z SE4      # resumable, per month
uv run python -m pricefc ingest weather historical_forecast --zone SE3   # training history
uv run python -m pricefc ingest weather previous_runs --zone SE3         # true-lead, backtests
uv run python -m pricefc ingest weather forecast --zone SE3              # live, archived as issued
uv run python -m pricefc ingest entsoe --zone SE3                        # needs ENTSOE_API_TOKEN
uv run python -m pricefc ingest entsoe --price-area NO1 --price-area FI  # neighbour prices
uv run python -m pricefc snapshots                                       # list raw snapshots
```

Every pull writes an immutable snapshot under `data/raw/` with a `manifest.json` and is logged
to the MLflow `ingest` experiment. Validation failures are kept but marked, and the command
exits non-zero.

## Querying the data

Build the cleaned silver tables and inspect them with DuckDB:

```bash
uv run python -m pricefc lake build
uv run python -m pricefc lake tables
uv run python -m pricefc lake sql "SELECT zone, timestamp, price_eur_mwh FROM prices WHERE zone = 'SE3' LIMIT 5"
```

The silver layer lives under `data/lake/silver/`. `prices` combines the available price zones;
individual tables such as `prices_se3` and `weather_previous_runs_se3_gavle` are queryable too.
Forecast endpoint rows retain `_pulled_at` so a forecast vintage can be selected explicitly;
revision rows are available through each table's `<table>_conflicts` view.
See `docs/decisions.md` for the merge and revision rules.

Weather data: [Open-Meteo](https://open-meteo.com/), CC BY 4.0.
Electricity prices: [elprisetjustnu.se](https://www.elprisetjustnu.se/).

`make test` runs offline tests only; `make test-live` checks the real APIs.
