# Regime-aware ensemble backtest

CIs use a circular seven-day block bootstrap.
DM tests compare daily pinball with the champion.
Regime labels exceed the prior expanding q90, after a 28-day warm-up.
Wind score: absolute day-over-day change in daily mean wind forecast.
The wind series uses `wz_wind_speed_100m_mean`.

| Zone | Slice | Model | Days | Pinball (95% CI) | Diff vs champion (95% CI) | DM p |
|---|---|---|---:|---:|---:|---:|
| SE3 | overall | ensemble_hourly_exp | 368 | 5.262 [4.865, 5.674] | 0.000 [0.000, 0.000] | — |
| SE3 | overall | ensemble_hourly_exp_disagree | 368 | 5.270 [4.879, 5.686] | 0.009 [0.001, 0.018] | 0.04945 |
| SE3 | overall | ensemble_hourly_exp_weather | 368 | 5.258 [4.867, 5.671] | -0.004 [-0.023, 0.014] | 0.6847 |
| SE3 | wind_change_top_decile | ensemble_hourly_exp | 27 | 5.958 [4.388, 8.275] | 0.000 [0.000, 0.000] | — |
| SE3 | wind_change_top_decile | ensemble_hourly_exp_disagree | 27 | 5.962 [4.399, 8.271] | 0.004 [-0.022, 0.030] | 0.7714 |
| SE3 | wind_change_top_decile | ensemble_hourly_exp_weather | 27 | 5.875 [4.364, 8.119] | -0.083 [-0.215, 0.033] | 0.1947 |
| SE3 | member_disagreement_top_decile | ensemble_hourly_exp | 33 | 7.216 [5.495, 8.991] | 0.000 [0.000, 0.000] | — |
| SE3 | member_disagreement_top_decile | ensemble_hourly_exp_disagree | 33 | 7.211 [5.518, 8.950] | -0.005 [-0.053, 0.041] | 0.8451 |
| SE3 | member_disagreement_top_decile | ensemble_hourly_exp_weather | 33 | 7.269 [5.523, 9.058] | 0.053 [-0.045, 0.146] | 0.2945 |
| SE3 | night_00_05 | ensemble_hourly_exp | 368 | 2.989 [2.633, 3.405] | 0.000 [0.000, 0.000] | — |
| SE3 | night_00_05 | ensemble_hourly_exp_disagree | 368 | 2.992 [2.639, 3.406] | 0.003 [-0.009, 0.016] | 0.6541 |
| SE3 | night_00_05 | ensemble_hourly_exp_weather | 368 | 2.997 [2.641, 3.412] | 0.007 [-0.001, 0.016] | 0.1135 |
| SE4 | overall | ensemble_hourly_exp | 368 | 6.413 [5.875, 7.055] | 0.000 [0.000, 0.000] | — |
| SE4 | overall | ensemble_hourly_exp_disagree | 368 | 6.415 [5.879, 7.058] | 0.002 [-0.016, 0.018] | 0.7831 |
| SE4 | overall | ensemble_hourly_exp_weather | 368 | 6.414 [5.878, 7.055] | 0.001 [-0.016, 0.016] | 0.9456 |
| SE4 | wind_change_top_decile | ensemble_hourly_exp | 34 | 6.331 [5.271, 7.819] | 0.000 [0.000, 0.000] | — |
| SE4 | wind_change_top_decile | ensemble_hourly_exp_disagree | 34 | 6.332 [5.259, 7.818] | 0.001 [-0.032, 0.036] | 0.9653 |
| SE4 | wind_change_top_decile | ensemble_hourly_exp_weather | 34 | 6.334 [5.271, 7.818] | 0.003 [-0.063, 0.074] | 0.9338 |
| SE4 | member_disagreement_top_decile | ensemble_hourly_exp | 40 | 8.594 [6.422, 11.070] | 0.000 [0.000, 0.000] | — |
| SE4 | member_disagreement_top_decile | ensemble_hourly_exp_disagree | 40 | 8.555 [6.455, 10.959] | -0.039 [-0.161, 0.041] | 0.423 |
| SE4 | member_disagreement_top_decile | ensemble_hourly_exp_weather | 40 | 8.576 [6.407, 11.076] | -0.018 [-0.105, 0.060] | 0.7636 |
| SE4 | night_00_05 | ensemble_hourly_exp | 368 | 4.064 [3.497, 4.720] | 0.000 [0.000, 0.000] | — |
| SE4 | night_00_05 | ensemble_hourly_exp_disagree | 368 | 4.076 [3.508, 4.727] | 0.012 [-0.005, 0.031] | 0.2476 |
| SE4 | night_00_05 | ensemble_hourly_exp_weather | 368 | 4.071 [3.505, 4.724] | 0.007 [-0.009, 0.022] | 0.4205 |
| SE2 | overall | ensemble_hourly_exp | 368 | 4.505 [3.820, 5.203] | 0.000 [0.000, 0.000] | — |
| SE2 | overall | ensemble_hourly_exp_disagree | 368 | 4.516 [3.829, 5.212] | 0.010 [-0.003, 0.023] | 0.08419 |
| SE2 | overall | ensemble_hourly_exp_weather | 368 | 4.503 [3.819, 5.197] | -0.003 [-0.019, 0.014] | 0.8004 |
| SE2 | wind_change_top_decile | ensemble_hourly_exp | 30 | 4.547 [3.353, 5.939] | 0.000 [0.000, 0.000] | — |
| SE2 | wind_change_top_decile | ensemble_hourly_exp_disagree | 30 | 4.555 [3.369, 5.950] | 0.008 [-0.019, 0.034] | 0.6061 |
| SE2 | wind_change_top_decile | ensemble_hourly_exp_weather | 30 | 4.531 [3.355, 5.925] | -0.016 [-0.088, 0.056] | 0.6905 |
| SE2 | member_disagreement_top_decile | ensemble_hourly_exp | 36 | 8.130 [6.314, 10.082] | 0.000 [0.000, 0.000] | — |
| SE2 | member_disagreement_top_decile | ensemble_hourly_exp_disagree | 36 | 8.197 [6.360, 10.155] | 0.068 [-0.018, 0.150] | 0.1141 |
| SE2 | member_disagreement_top_decile | ensemble_hourly_exp_weather | 36 | 8.125 [6.263, 10.086] | -0.004 [-0.138, 0.111] | 0.9427 |
| SE2 | night_00_05 | ensemble_hourly_exp | 368 | 2.278 [1.897, 2.708] | 0.000 [0.000, 0.000] | — |
| SE2 | night_00_05 | ensemble_hourly_exp_disagree | 368 | 2.282 [1.900, 2.707] | 0.004 [-0.005, 0.012] | 0.4256 |
| SE2 | night_00_05 | ensemble_hourly_exp_weather | 368 | 2.287 [1.910, 2.712] | 0.009 [-0.001, 0.020] | 0.1406 |
| SE1 | overall | ensemble_hourly_exp | 368 | 4.590 [3.946, 5.247] | 0.000 [0.000, 0.000] | — |
| SE1 | overall | ensemble_hourly_exp_disagree | 368 | 4.608 [3.965, 5.265] | 0.018 [0.005, 0.031] | 0.006279 |
| SE1 | overall | ensemble_hourly_exp_weather | 368 | 4.597 [3.949, 5.263] | 0.007 [-0.015, 0.035] | 0.4543 |
| SE1 | wind_change_top_decile | ensemble_hourly_exp | 27 | 4.587 [3.159, 6.000] | 0.000 [0.000, 0.000] | — |
| SE1 | wind_change_top_decile | ensemble_hourly_exp_disagree | 27 | 4.596 [3.166, 6.000] | 0.009 [-0.018, 0.043] | 0.5285 |
| SE1 | wind_change_top_decile | ensemble_hourly_exp_weather | 27 | 4.600 [3.157, 6.006] | 0.013 [-0.047, 0.088] | 0.716 |
| SE1 | member_disagreement_top_decile | ensemble_hourly_exp | 35 | 7.500 [6.524, 8.528] | 0.000 [0.000, 0.000] | — |
| SE1 | member_disagreement_top_decile | ensemble_hourly_exp_disagree | 35 | 7.527 [6.569, 8.524] | 0.027 [-0.034, 0.090] | 0.3993 |
| SE1 | member_disagreement_top_decile | ensemble_hourly_exp_weather | 35 | 7.505 [6.470, 8.558] | 0.005 [-0.141, 0.206] | 0.9489 |
| SE1 | night_00_05 | ensemble_hourly_exp | 368 | 2.144 [1.814, 2.496] | 0.000 [0.000, 0.000] | — |
| SE1 | night_00_05 | ensemble_hourly_exp_disagree | 368 | 2.150 [1.821, 2.498] | 0.005 [-0.008, 0.019] | 0.4114 |
| SE1 | night_00_05 | ensemble_hourly_exp_weather | 368 | 2.146 [1.818, 2.498] | 0.002 [-0.006, 0.009] | 0.5938 |

## Evaluation coverage

- SE3: origins 2025-10-03 to 2026-10-05; 27 wind-change days and 33 disagreement days met the causal top-decile rule.
- SE4: origins 2025-10-03 to 2026-10-05; 34 wind-change days and 40 disagreement days met the causal top-decile rule.
- SE2: origins 2025-10-03 to 2026-10-05; 30 wind-change days and 36 disagreement days met the causal top-decile rule.
- SE1: origins 2025-10-03 to 2026-10-05; 27 wind-change days and 35 disagreement days met the causal top-decile rule.
