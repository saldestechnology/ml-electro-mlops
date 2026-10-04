# Decisions log

## 2026-09-30 — M0 skeleton
- Python 3.12 (broadest compatibility for LightGBM, StatsForecast and TimesFM/PyTorch).
- Package name `pricefc`, uv-managed with `uv.lock` committed (D7).
- Local MLflow: SQLite backend (`mlflow.db`) plus local artifacts (`mlruns/`) until the shared server exists (D9).
- Config: YAML in `configs/`, validated with pydantic; the resolved config is hashed (`config_hash` tag).
- Heavy dependencies (LightGBM, StatsForecast, Optuna, Prefect, TimesFM) are added in the milestone that first needs them.

## 2026-09-30 — M1 ingestion (partial: ENTSO-E token pending)
- **Raw storage**: local `data/raw/` for now (D8 still open). Layout and manifests follow spec 5.3;
  snapshot files are written read-only and a snapshot directory is never reused.
- **Invalid snapshots are kept**, marked `validation.passed=false` in the manifest; readers
  (`load_latest`) skip them. The raw record of what the API returned is preserved, and the CLI
  exits non-zero so downstream flows stop.
- **Open-Meteo model pinned to `ecmwf_ifs`**. `best_match` and `*_seamless` switch underlying
  models over time (and `best_match` lacks 100 m wind in Previous Runs), which breaks
  reproducibility. `ecmwf_ifs` is continuous in the Historical Forecast API from before 2021 and
  has Previous Runs from 2025-10-01 (day-1 lead) / 2025-10-02 (day-2 lead), so training
  (stitched) and backtest (true-lead) use the same model. Tag: `open-meteo:<endpoint>:ecmwf_ifs`.
- **Train/eval weather mismatch (intended)**: training uses the stitched historical-forecast
  series (slightly optimistic); backtests must use Previous Runs. The true-lead window
  (2025-10-02 onwards) coincides with the 12-month backtest window.
- **Previous Runs archive gaps**: whole days are missing between about 2026-03-26 and
  2026-05-06, differently per variable and lead. Day-1 and day-2 gaps mostly do not overlap,
  but `temperature_2m` is missing in both for 366 hours (and 100 m wind for ~263 hours).
  Tolerated in validation via `max_null_frac: 0.10` (per-endpoint config) with per-column
  `null_counts` recorded. M2 must handle this explicitly (lead fallback day1 -> day2, then flag
  or exclude affected origins); never silently impute.
- **Leakage note for M2**: `previous_day1` values for early D+1 hours may come from a run whose
  data is only available shortly before the 09:00 origin. Define `available_at` from model run
  time + publication delay; if unclear, use `previous_day2` for strictness.
- **ENTSO-E**: entsoe-py 0.8.1. All series converted to UTC; per-row `resolution` inferred from
  spacing (handles the 2025-10-01 switch to PT15M). Chunks are [start, end) to avoid the
  library's inclusive-end overlap. Tests use a fake client; no recorded ENTSO-E fixtures yet
  (need a token). Hydro reservoirs are pulled per country (`SE`) [VERIFY].
- **Live forecast archive** started 2026-09-30 (as-issued vintages, spec 11).
- **First SE3 weather backfill has no commit SHA**: the 20 snapshots pulled on 2026-09-30 before
  the first commit record `git_sha: unknown, git_dirty: true`. They are kept, but superseded by a
  re-pull made from a committed tree, and dataset builds should use the re-pulled snapshots.

## 2026-09-30 — Prices from elprisetjustnu.se (ENTSO-E token pending)
- **Target source**: `prices.source: elprisetjustnu` in `configs/ingest.yaml`. Pluggable
  `PriceSource` (elprisetjustnu, entsoe) with one output schema; the snapshot `source` records
  which produced each series. ENTSO-E remains the intended source of record; when the token
  arrives, pull the same days from both and compare (this doubles as real-API validation).
- **Coverage**: 2021-11-01 onwards for SE1-SE4 (probed; 2021-10-31 is 404). Prices exclude VAT
  and taxes. EUR/kWh x 1000 = EUR/MWh (5 decimals, so exact to 0.01). Attribution to
  elprisetjustnu.se is recorded in every manifest and required for public use.
- **Snapshot granularity**: one snapshot per zone and calendar month (~240 total), not per day.
  Resumable: complete months with a valid snapshot are skipped; the current month is re-pulled.
- **API quirks**: the default Python user agent gets HTTP 403, so the client sends its own.
  `time_end` is computed in wall-clock time, so the last slot before the autumn DST change
  claims 75 minutes; resolution is therefore derived from `time_start` spacing and
  inconsistent `time_end` values are listed in the manifest (`time_end_anomalies`).
- **Energy crisis**: history starts in the 2021-22 price crisis. Report backtest scores with and
  without that period (decide in M3 whether evaluation starts after it).
- **Live tests**: `make test-live` hits the real APIs (one day per dataset); skipped by default.
- **Negative night-time radiation**: Previous Runs returned `shortwave_radiation = -1 W/m2` at
  night (SE1/SE2, 2025-10-28 20:00Z). Radiation now allows down to -5 W/m2 with a warning below
  0; values are kept as delivered (clipping, if any, belongs in feature building).

## 2026-10-01 — Price backfill verified against independent sources
- **Backfill**: elprisetjustnu.se, SE1-SE4, 2021-11-01 to 2026-09-30: 233 of 236 zone-months
  valid.
- **Nord Pool** (the exchange; its public data portal API is open only for about the last two
  months): 40 zone-days in Aug-Sep 2026, all four zones, 3,840 quarter-hours. Identical to the
  cent.
- **Energy-Charts** (licence: private/internal use only; used for this check only, not as a
  data source): 12 stress days x 4 zones (DST days in both hourly and 15-minute eras, the
  2025-10-01 switch, the Aug 2022 crisis peak, ordinary days), 2,015 intervals. 47 of 48
  zone-days identical, and the one exception is a known gap day.
- **Source defect found**: on 5 days elprisetjustnu dropped one hour mid-day and shifted every
  later hour one slot earlier (the last hour is empty): SE3 2021-11-04 (from 13:00, up to
  53 EUR/MWh off), SE2 2022-09-04, 2022-09-10, 2022-09-15 and 2022-10-20 (up to 139 EUR/MWh
  off). All five have 23 rows, so validation catches them, and their months (SE3 2021-11,
  SE2 2022-09, SE2 2022-10) are marked invalid. **These values are wrong, not just
  incomplete; never repair them by filling the missing hour.** Replace those days from ENTSO-E
  once the token is available.
- **Residual risk**: a shift inside a day that keeps the correct row count would pass
  validation. The sample showed none, but full coverage needs the ENTSO-E comparison of every
  day. Do it when the token arrives, before the price series is used as the source of record.
- **Known-bad days are excluded at ingest** (`elprisetjustnu.known_bad_days` in
  `configs/ingest.yaml`). Those days are still fetched (the manifest records how many rows the
  source returned, which shows if it gets corrected) but their rows are dropped, never
  repaired. Validation requires them to be empty and allows the gap they leave. Their months
  validate again, so daily runs exit cleanly and any *new* defect still fails. Datasets lack
  these 5 days until ENTSO-E replaces them.

## 2026-10-01 — M2 datasets
- **Rows**: one per (origin day D at 09:00 Europe/Stockholm, target hour of local D+1); 23/24/25
  rows per origin. Target `y` = hourly mean price (15-minute MTUs averaged, D2).
- **Availability rules** (every input declares one; features only see as-of views):
  prices for delivery day X known from X-1 13:00 local (conservative vs ~12:45 CET);
  weather value for T known from T - 48h + 8h publication delay.
- **Weather lead changed to day 2** (supersedes the earlier "day-1 then day-2 fallback" note).
  Open-Meteo defines previous_dayN as "predicted N x 24 hours before valid time"; with an 8h
  publication delay, day-1 values would be available after the origin for most D+1 hours.
  Day-2 is available for all of them. The Single Runs API could tighten this later.
- **Stitched training weather is treated as a day-2 forecast** (its true issue time is
  unknown). Measured on the 8,664 overlapping SE3 rows vs true day-2 lead: temperature
  corr 0.996 (MAE 0.5 C), radiation 0.991, **100 m wind 0.939 (MAE 0.6 m/s)**. Expect models
  to look better in training than in the true-lead backtest, mostly through wind.
- **Leakage audit** runs on every build: seeded random origins + both ends + every origin with
  a 23/25-hour target day; all data unavailable at the origin is perturbed and every feature
  must be bit-identical; the perturbation must also change the target (non-vacuous). The build
  fails otherwise. Plus a Hypothesis invariant test and injected-leak tests, mutation-checked.
- **Holidays**: `holidays` package, Sweden, with Sundays removed from "public" and de facto
  days (Midsummer, Christmas and New Year's Eve), half days ("from 2pm") and computed bridge
  days as separate features.
- **Versioning**: `data/datasets/{zone}/hourly/{YYYYMMDD}-{digest6}/` with a lineage manifest
  (contributing raw snapshots with hashes and git SHAs, availability rules, audit report).
  Timestamp columns are normalised to ns so the digest does not depend on pandas unit inference.
- **Built 2026-10-01** (all leakage audits passed):
  stitched SE1-SE4: origins 2021-11-08..2026-09-28 (~42.9k rows; SE2 -96 for excluded days);
  true_lead SE1-SE4: origins 2025-10-03..2026-09-28 (8,664 rows; 7.5% of SE3 rows lack some
  weather from the archive gap, none lack all).
- **Not yet in datasets**: ENTSO-E fundamentals (load/wind/solar forecasts, flows, outages,
  hydro). They will be a second dataset version, so their value can be measured.
- **Holiday names are locale-dependent** (found by CI): the `holidays` package localises names
  from the system locale, and the calendar features match on names ("Sunday", "(from 2pm)").
  Under a Swedish/C locale, every Sunday became a public holiday and half days vanished. Fixed
  by pinning `language="en_US"`; a test runs the calendar under three locales. Datasets built
  on 2026-10-01 were built under en_US and are unaffected (rebuilt SE3 digests identical).

## 2026-10-04 — No ENTSO-E token (owner decision)
- The owner chose not to apply for an ENTSO-E token. elprisetjustnu.se is the price source of
  record. The 5 known-bad days stay excluded (no replacement source), and the full every-day
  cross-check against ENTSO-E will not happen; the sample verification (Nord Pool, Energy-
  Charts) is the evidence. ENTSO-E fundamentals (load/wind/solar forecasts, flows, outages,
  hydro) are out of scope unless a token is obtained later; the client code stays.

## 2026-10-04 — M3 backtest harness and baselines
- **Harness**: rolling origin over the true-lead dataset; fit on stitched-dataset rows with
  target_date <= origin day (published before the origin); monthly refit by default,
  `every_origin` for models that need the latest history. Quantile crossing fixed by sorting;
  NaN quantiles fail the run.
- **Baselines**: seasonal naive 7d (the spec's reference), seasonal naive 1d, the standard EPF
  `naive_weekday` (1d for Tue-Fri, 7d for Sat-Mon), and StatsForecast MSTL(24,168)+AutoETS.
  Naive quantiles = empirical per-hour quantiles of the method's own errors (last 56 days).
- **Dev-mode bias found**: `dev_every_n_days: 7` sampled only Fridays (every target a Saturday),
  which flipped the ranking of the 1d and 7d naives. Now 5, and multiples of 7 are rejected.
- **Statistics**: circular block bootstrap (7-day blocks, 2,000 resamples, percentile 95% CI)
  on daily losses; Diebold-Mariano with the HLN correction on daily loss differentials.
- **sMAPE** denominator floored at 1 EUR/MWh (near-zero and negative prices).
- **Crisis**: the evaluation window (Oct 2025 - Sep 2026) contains no 2022-23 crisis data, so
  "with/without crisis" is a training-data question (`train_start`), compared in M4.
