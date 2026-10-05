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
- **Baseline results** (full backtest, 365 origins 2025-10-03..2026-10-02, datasets
  2026-10-04; mean pinball EUR/MWh with 95% block-bootstrap CI; skill vs seasonal naive 7d):

  | Zone | MSTL+ETS | Naive 1d | EPF weekday naive | Naive 7d (ref) | MSTL skill |
  |------|----------|----------|-------------------|----------------|------------|
  | SE1 | 5.95 (5.31-6.65) | 7.70 | 8.41 | 11.12 | 0.47 |
  | SE2 | 6.22 (5.53-6.97) | 8.02 | 8.80 | 11.48 | 0.46 |
  | SE3 | 6.53 (6.09-6.98) | 9.15 | 9.16 | 11.61 | 0.44 |
  | SE4 | 8.35 (7.63-9.14) | 11.41 | 11.52 | 13.69 | 0.39 |

  Every model beats the 7d reference with DM p < 0.001. MSTL is the bar for M4. Interval
  coverage (80/90%): MSTL 0.78-0.83 / 0.85-0.90 (close to nominal); naives ~0.74 / ~0.84
  (too narrow). The EPF weekday naive does not beat the plain 1d naive here.

## 2026-10-04 — M4 LightGBM (SE3)
- **Tuning** (Optuna, 40 trials, 28 complete / 12 pruned, 46 min): 4 expanding folds of 91 days
  ending 2025-10-02, i.e. before the evaluation window, asserted in code. Objective: mean
  pinball over q10/q50/q90 (subset for speed). Best: lr 0.025, 252 leaves, min_child 263,
  colsample 0.63, subsample 0.75, n_estimators 220 (median early-stopping iteration).
  Learning rate and min_child_samples explain ~80% of the variance between trials. Validation
  folds use stitched weather (no true-lead data exists before Oct 2025).
- **Backtest, SE3, 365 origins** (mean pinball EUR/MWh):
  tuned+recalibrated(28d) 5.727 | tuned 5.753 | untuned 5.845 | tuned, trained from 2023-07
  6.088 | MSTL 6.530 | naive 7d 11.613.
  vs MSTL: -0.78 EUR/MWh, 95% block-bootstrap CI [-1.15, -0.40], DM p < 0.001.
  tuned vs untuned: -0.09, CI [-0.19, +0.005], DM p = 0.017 (small, borderline).
- **Crisis years help**: training from 2023-07-01 instead of all history is clearly worse
  (6.09 vs 5.75), so `train_start` stays null.
- **Calibration problem and fix**: raw LightGBM was overconfident on true-lead data (80/90%
  intervals covered 72/84%, bias -5.7 EUR/MWh, 22% of rows had crossing quantiles fixed by
  sorting), consistent with training on optimistic stitched weather. A rolling per-quantile
  recalibration (offsets from the model's own forecasts of the last 28 published days) gives
  coverage 77.6/87.9%, bias -2.1, and the best pinball. A 14-day window is worse (too noisy).
  The first ~7 days of a deployment are uncalibrated (cold start).
- **Seed variance** (6 seeds, recalibrated): pinball 5.730 +/- 0.009 (sd), coverage
  0.777/0.881 +/- 0.002, i.e. ~100x smaller than the margin over MSTL.
- **Feature importance** (gain, median model): own price lags/stats 55% (p_lag1d alone 26%),
  weather 27% (100 m wind leads), calendar 12%, neighbour prices 6%.
- **Leakage hardening**: `predict()` no longer receives y or its metadata (test enforced).
- **Bugs found and fixed while running M4**: comparison keyed variants by base name (a variant
  overwrote its parent; regression test fails on the old code); MLflow logged base params for
  variants; '@'/'=' invalid in MLflow metric names; `compare_runs` set-pop in a generator.

## 2026-10-04 — M4 LightGBM (SE1, SE2, SE4) and champion candidates
- **Tuning** (same setup as SE3, 40 trials each, ~45 min per zone): the other zones prefer
  small, heavily regularised trees (17-29 leaves, min_child 236-392, colsample 0.41-0.66,
  reg_alpha 1-2.7) versus 252 leaves for SE3. n_estimators: SE1 290, SE2 340, SE4 760.
  Fold pinball (q10/q50/q90): SE1 3.29, SE2 3.23, SE4 6.97.
- **Backtest, 365 true-lead origins per zone** (mean pinball EUR/MWh; diff vs MSTL with 95%
  block-bootstrap CI; coverage 80/90%; bias = mean(q50 - y)):

  | zone | tuned+recal(28d) | tuned | MSTL | naive 7d | diff vs MSTL [CI] | DM p | cov 80/90 | bias |
  |---|---|---|---|---|---|---|---|---|
  | SE1 | 5.053 | 5.060 | 5.950 | 11.117 | -0.90 [-1.26, -0.54] | 2e-7 | 76.9/88.3 | -3.4 |
  | SE2 | 4.788 | 4.790 | 6.216 | 11.479 | -1.43 [-1.75, -1.11] | 7e-15 | 78.4/88.8 | -4.0 |
  | SE3 | 5.727 | 5.753 | 6.530 | 11.613 | -0.78 [-1.15, -0.40] | <1e-3 | 77.6/87.9 | -2.1 |
  | SE4 | 6.920 | 7.011 | 8.351 | 13.691 | -1.43 [-1.89, -1.00] | 8e-12 | 77.7/88.7 | -1.7 |

- **Recalibration** barely moves pinball in SE1/SE2 (<0.01) but fixes coverage everywhere
  (raw: 68-72% / 82-84%), so the recalibrated variant is the candidate in all zones.
- **Remaining weakness**: q50 still under-forecasts by 2-4 EUR/MWh on average (MSTL ~0). The
  28-day offset is a quantile of residuals, so it does not target mean bias, and the right
  tail of prices (spikes) pulls the mean above the median. Not a promotion blocker under the
  pinball criterion; worth revisiting with M8 ensembling.
- **Champion candidates (M4)**: `lightgbm_tuned_{zone}@calibration=28` in every zone. All
  criteria met: better mean pinball than the incumbent best (MSTL), paired CI excludes zero,
  DM significant, coverage within 3 points of nominal, leakage audits clean. Formal
  registration and the champion alias happen in M6 (requires the pyfunc wrapper).
- `backtest run` / `backtest compare` now print the paired difference CI columns.

## 2026-10-05 — M5 TimesFM (zero-shot, covariates)
- **Versions verified** (PyPI `timesfm` 3.0.2; Hugging Face): TimesFM 2.5 200M
  (`google/timesfm-2.5-200m-pytorch@1d952420…`, Apache-2.0, up to 16k context, deciles,
  covariates via XReg = in-context linear regression + TimesFM on residuals) and TimesFM 3.0
  (`google/timesfm-3.0-pytorch@43046b85…`, native past/future covariates, MLX backend).
- **Licence decision (owner, 2026-10-04)**: 3.0 weights are `timesfm-non-commercial-license-v1.0`
  (no production or commercial use; fine-tunes are derivatives under the same terms). 3.0 is a
  research benchmark only: guarded by `accept_noncommercial_licence`, runs tagged
  `deployable=false`, never registered or served. 2.5 is the deployable candidate.
- **Mac smoke test**: 2.5 torch CPU 0.24 s/forecast (MPS slower, 1.8 s); 3.0 MLX 0.05 s.
  `infer_is_positive` must be off (it clamps forecasts at 0 when the context has no negative
  prices). No GPU needed for zero-shot: a full-year backtest is 1.5-5 min per model.
- **Deadlock found**: LightGBM and torch ship separate libomp copies on macOS; torch
  inference hangs after a LightGBM fit in the same process. Fixed by one torch thread.
- **Adapter**: context = last 2048 published hours (ends at local midnight before D+1);
  deciles mapped to our quantiles (interpolation, normal-scaled tails). Covariates = zone
  weather means (temperature, 100 m wind, radiation, cloud) + calendar; future values from the
  leakage-audited feature rows, past values from training rows (stitched weather).
- **Results, 365 true-lead origins** (mean pinball; diff vs tuned LightGBM+recal, 95% CI):

  | zone | LGBM+recal | 3.0+cov | diff [CI] | 3.0 | 2.5 | diff 2.5 [CI] |
  |---|---|---|---|---|---|---|
  | SE1 | 5.053 | **4.054** | -1.00 [-1.28, -0.72] | 4.792 | 4.927 | -0.13 [-0.42, +0.19] |
  | SE2 | 4.788 | **3.866** | -0.92 [-1.21, -0.66] | 5.082 | 5.188 | +0.40 [+0.14, +0.67] |
  | SE3 | 5.727 | **4.989** | -0.74 [-1.01, -0.45] | 5.757 | 6.118 | +0.39 [+0.07, +0.71] |
  | SE4 | 6.920 | **6.059** | -0.86 [-1.11, -0.62] | 7.110 | 7.505 | +0.59 [+0.24, +0.95] |

  DM p < 0.001 for 3.0+cov in every zone. Coverage 80/90 without recalibration: 3.0+cov
  76-80% / 86-88%, 2.5 77-80% / 86-88%; recalibration does not help TimesFM (SE3: 3.0+cov
  4.989 -> 5.035, 2.5 6.118 -> 6.215).
- **2.5 with XReg covariates is worse than 2.5 alone** (dev SE3 5.99 vs 5.45): the linear
  in-context regression does not capture the weather-price relation. Dropped.
- **Dev-mode caution**: the 73-origin dev sample ranked 2.5 level with LightGBM (5.45 vs
  5.47); over the full year 2.5 is clearly worse in SE2-SE4. Full runs decide.
- **Slices (SE3)**: 2.5 beats LightGBM strongly at night (hours 0-5: 2.5-3.9 vs 4.4-4.9),
  on negative-price hours and in winter/spring; LightGBM wins hours 7-23 and spike days.
  Complementary errors: a strong case for the M8 ensemble of the two deployable models.
- **Fine-tuning decision**: not now. 3.0 already wins but cannot be deployed, and a fine-tune
  would inherit its licence. 2.5 meets the spec's slice precondition, but the free option
  (M8 ensemble LightGBM + 2.5, possibly per hour) should be tried first; fine-tuning 2.5 on
  RunPod only if a clear gap to 3.0+cov remains, and only with owner approval of the cost.
- 3.0+cov shows how much headroom exists (~15% over LightGBM); it is the benchmark the
  deployable stack should approach.

## 2026-10-05 — M8 ensembles
- **Design**: `EnsembleForecaster` is a `Forecaster` in the harness. Members refit on their own
  cadence; the ensemble averages their sorted quantiles with convex weights chosen by mean
  pinball on the members' *own past forecasts* of days published by the origin (same causal
  pattern as recalibration). Simplex grid, step 0.05; equal weights until 14 days of history.
  Schemes: equal, global, hourly (per local hour); trailing 56 days or expanding window.
  Member forecasts are memoised per process so variants share the members' work.
- **Bug found**: member specs (`@`, `=`) made MLflow param names invalid; the first SE3 run
  crashed after one variant. Sanitised; test checks names against MLflow's validator.
- **Results, 365 origins** (mean pinball; diff vs tuned LightGBM+recal, 95% CI; DM p<0.001
  unless noted):

  | zone | LGBM+recal | equal | hourly 56d | **hourly expanding** | diff [CI] | research (+3.0cov) | 3.0+cov |
  |---|---|---|---|---|---|---|---|
  | SE1 | 5.053 | 4.701 | 4.630 | **4.608** | -0.45 [-0.61, -0.29] | 4.086 | 4.054 |
  | SE2 | 4.788 | 4.690 (p=0.16) | 4.552 | **4.528** | -0.26 [-0.35, -0.17] | 3.883 | 3.866 |
  | SE3 | 5.727 | 5.430 | 5.273 | **5.248** | -0.48 [-0.60, -0.36] | 4.873 | 4.989 |
  | SE4 | 6.920 | 6.581 | 6.408 | **6.390** | -0.53 [-0.67, -0.38] | 5.964 | 6.059 |

  Coverage (hourly expanding) 79.5-82.2% / 88.8-90.4%; median bias unchanged (-1.6 to -4.3).
  Spike days improve in every zone (-1% to -9%); only SE1 negative-price hours get worse
  (+5.8%, small slice).
- **Learned weights (SE3)**: TimesFM 2.5 gets 0.9-0.95 for hours 0-5 and 0.1-0.35 from the
  morning ramp onwards; stable over the year. Per-hour weighting is what matters (global
  weights are no better than equal).
- **Gap to the benchmark**: the deployable ensemble closes 30-65% of the gap between LightGBM
  and TimesFM 3.0+cov (SE3 65%, SE4 62%, SE1 45%, SE2 28%). The research ensemble with 3.0
  is on par with or better than 3.0 alone (not deployable).
- **Champion candidates (M8)**: `ensemble_hourly_exp` (LightGBM+recal and TimesFM 2.5, both
  deployable) in every zone: better than the M4 candidate with CI excluding zero, DM
  significant, coverage within 2.5 points of nominal, no degradation on spike days, members'
  leakage audits clean. To be registered in M6.
- **Fine-tuning still deferred**: the remaining gap to 3.0+cov is 0.4-0.7 EUR/MWh. A cheaper
  next lever is the LightGBM night-hour feature (last published hours of day D).
