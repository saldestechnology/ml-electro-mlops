"""Build the report figures from logged MLflow runs, datasets and the Optuna store.

Run from the repo root:  uv run python docs/report/make_figures.py
Every number plotted comes from a full-year (365-origin) backtest run in MLflow.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mlflow
import numpy as np
import optuna
import pandas as pd

from pricefc.backtest.run import latest_dataset
from pricefc.backtest.stats import block_bootstrap_mean
from pricefc.config import load_config

OUT = Path("docs/report/figures")
OUT.mkdir(parents=True, exist_ok=True)
ZONES = ["SE1", "SE2", "SE3", "SE4"]
plt.rcParams.update(
    {"font.size": 9, "axes.spines.top": False, "axes.spines.right": False, "figure.dpi": 150}
)
mlflow.set_tracking_uri("sqlite:///mlflow.db")
BASE = load_config(Path("configs/base.yaml"))

MODELS = {  # label -> run-name template
    "Naive 7d": "{z}-seasonal_naive_7d",
    "MSTL+ETS": "{z}-mstl_ets",
    "LightGBM+recal": "{z}-lightgbm_tuned_{z}@calibration=28",
    "TimesFM 2.5": "{z}-timesfm25",
    "TimesFM 3.0": "{z}-timesfm3",
    "TimesFM 3.0+cov": "{z}-timesfm3_cov",
    "Ensemble (deployable)": "{z}-ensemble_hourly_exp",
}
COLORS = {
    "Naive 7d": "#9e9e9e",
    "MSTL+ETS": "#8c6d31",
    "LightGBM+recal": "#1f77b4",
    "TimesFM 2.5": "#2ca02c",
    "TimesFM 3.0": "#bcbd22",
    "TimesFM 3.0+cov": "#d62728",
    "Ensemble (deployable)": "#9467bd",
}


def run_id(name: str) -> str:
    df = mlflow.search_runs(
        experiment_names=["backtest"],
        filter_string=f"tags.mlflow.runName = '{name}' and params.origins = '365'",
        order_by=["attributes.start_time DESC"],
        max_results=1,
    )
    if df.empty:
        raise LookupError(name)
    return str(df.run_id.iloc[0])


def artifact(name: str, path: str) -> pd.DataFrame:
    p = mlflow.artifacts.download_artifacts(run_id=run_id(name), artifact_path=path)
    return pd.read_parquet(p) if p.endswith(".parquet") else pd.read_csv(p)


def save(fig: plt.Figure, name: str) -> None:
    fig.tight_layout()
    fig.savefig(OUT / f"{name}.pdf")
    plt.close(fig)
    print("wrote", name)


def fig_prices() -> None:
    fig, ax = plt.subplots(figsize=(7, 2.6))
    for z, c in zip(ZONES, ["#6baed6", "#3182bd", "#08519c", "#e6550d"], strict=True):
        ds = latest_dataset(BASE, z, "stitched").read()
        d = ds.groupby("target_date")["y"].mean()
        d.index = pd.to_datetime(d.index)
        ax.plot(d.rolling(7).mean(), lw=0.8, color=c, label=z)
    ax.axvspan(pd.Timestamp("2025-10-04"), pd.Timestamp("2026-10-03"), color="k", alpha=0.07)
    ax.text(pd.Timestamp("2025-11-01"), 300, "evaluation\nyear", fontsize=8)
    ax.set_ylabel("EUR/MWh (7-day mean)")
    ax.legend(ncol=4, frameon=False, loc="upper center")
    save(fig, "prices")


def fig_weather_gap() -> None:
    st = latest_dataset(BASE, "SE3", "stitched").read()
    tl = latest_dataset(BASE, "SE3", "true_lead").read()
    m = st.merge(tl, on="target_time", suffixes=("_s", "_t"))
    fig, axes = plt.subplots(1, 2, figsize=(6.5, 2.8))
    for ax, v, unit in zip(
        axes, ["wz_temperature_2m_mean", "wz_wind_speed_100m_mean"], ["°C", "m/s"], strict=True
    ):
        a, b = m[f"{v}_s"], m[f"{v}_t"]
        ok = a.notna() & b.notna()
        ax.scatter(b[ok], a[ok], s=1, alpha=0.15, color="#1f77b4")
        lo, hi = float(min(a[ok].min(), b[ok].min())), float(max(a[ok].max(), b[ok].max()))
        ax.plot([lo, hi], [lo, hi], "k--", lw=0.7)
        r = np.corrcoef(a[ok], b[ok])[0, 1]
        ax.set_title(f"{'100 m wind speed' if 'wind' in v else 'temperature'}, r = {r:.3f}")
        ax.set_xlabel(f"true day-2 lead ({unit})")
        ax.set_ylabel(f"stitched training series ({unit})")
    save(fig, "weather_gap")


def fig_zone_scores() -> pd.DataFrame:
    rows = []
    for z in ZONES:
        for label, tpl in MODELS.items():
            d = artifact(tpl.format(z=z), "daily_losses.csv")["pinball"].to_numpy()
            ci = block_bootstrap_mean(d, block_length=7, n_resamples=2000, seed=42)
            rows.append({"zone": z, "model": label, "mean": ci.estimate, "lo": ci.lo, "hi": ci.hi})
    t = pd.DataFrame(rows)
    fig, ax = plt.subplots(figsize=(7, 3))
    width = 0.115
    for i, label in enumerate(MODELS):
        s = t[t.model == label]
        x = np.arange(len(ZONES)) + (i - (len(MODELS) - 1) / 2) * width
        ax.bar(x, s["mean"], width, color=COLORS[label], label=label)
        ax.errorbar(
            x,
            s["mean"],
            yerr=[s["mean"] - s["lo"], s["hi"] - s["mean"]],
            fmt="none",
            ecolor="k",
            lw=0.6,
            capsize=1.5,
        )
    ax.set_xticks(np.arange(len(ZONES)), ZONES)
    ax.set_ylabel("mean pinball loss (EUR/MWh)")
    ax.legend(ncol=3, frameon=False, fontsize=7.5, loc="upper left")
    ax.set_ylim(0, t["hi"].max() * 1.25)
    save(fig, "zone_scores")
    return t


def fig_hourly() -> None:
    fig, axes = plt.subplots(1, 2, figsize=(7, 2.7), sharey=True)
    for ax, z in zip(axes, ["SE3", "SE4"], strict=True):
        for label in [
            "MSTL+ETS",
            "LightGBM+recal",
            "TimesFM 2.5",
            "TimesFM 3.0+cov",
            "Ensemble (deployable)",
        ]:
            s = artifact(MODELS[label].format(z=z), "slice_metrics.csv")
            s = s[s.slice == "hour"].assign(h=lambda d: d.group.astype(int)).sort_values("h")
            ax.plot(s.h, s.pinball_mean, marker="o", ms=2, lw=1, color=COLORS[label], label=label)
        ax.set_title(z)
        ax.set_xlabel("hour of delivery day (local)")
        ax.set_xticks(range(0, 24, 3))
    axes[0].set_ylabel("mean pinball (EUR/MWh)")
    axes[0].legend(frameon=False, fontsize=7)
    save(fig, "hourly")


def fig_reliability() -> None:
    taus = [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95]
    cols = ["q05", "q10", "q25", "q50", "q75", "q90", "q95"]
    fig, ax = plt.subplots(figsize=(3.4, 3.2))
    cands = {
        "LightGBM raw": "SE3-lightgbm_tuned_SE3",
        "LightGBM+recal": MODELS["LightGBM+recal"].format(z="SE3"),
        "TimesFM 2.5": MODELS["TimesFM 2.5"].format(z="SE3"),
        "TimesFM 3.0+cov": MODELS["TimesFM 3.0+cov"].format(z="SE3"),
    }
    style = {"LightGBM raw": ("#1f77b4", "--")}
    for label, name in cands.items():
        f = artifact(name, "forecasts.parquet")
        emp = [float((f.y <= f[c]).mean()) for c in cols]
        color, ls = style.get(label, (COLORS.get(label, "k"), "-"))
        ax.plot(taus, emp, marker="o", ms=3, lw=1, ls=ls, color=color, label=label)
    ax.plot([0, 1], [0, 1], "k:", lw=0.7)
    ax.set_xlabel("nominal quantile level")
    ax.set_ylabel("observed frequency y <= q")
    ax.set_title("SE3 quantile reliability")
    ax.legend(frameon=False, fontsize=7)
    save(fig, "reliability")


def fig_fan() -> None:
    lo, hi = "2026-01-12", "2026-01-25"
    fig, axes = plt.subplots(2, 1, figsize=(7, 4.2), sharex=True, sharey=True)
    for ax, label in zip(axes, ["LightGBM+recal", "TimesFM 3.0+cov"], strict=True):
        f = artifact(MODELS[label].format(z="SE3"), "forecasts.parquet")
        f = f[(f.target_date >= lo) & (f.target_date <= hi)].sort_values("target_time")
        t = pd.to_datetime(f.target_time).dt.tz_convert("Europe/Stockholm")
        ax.fill_between(t, f.q05, f.q95, color=COLORS[label], alpha=0.15, lw=0, label="90% PI")
        ax.fill_between(t, f.q25, f.q75, color=COLORS[label], alpha=0.35, lw=0, label="50% PI")
        ax.plot(t, f.q50, color=COLORS[label], lw=0.9, label="median")
        ax.plot(t, f.y, color="k", lw=0.8, label="actual")
        ax.set_title(f"SE3, {label}", fontsize=9)
        ax.set_ylabel("EUR/MWh")
    axes[0].legend(ncol=4, frameon=False, fontsize=7, loc="upper left")
    fig.autofmt_xdate()
    save(fig, "fan")


def fig_importance() -> None:
    fi = artifact(MODELS["LightGBM+recal"].format(z="SE3"), "feature_importance_gain.csv")
    fi = fi.rename(columns={fi.columns[0]: "feature"})

    def group(f: str) -> str:
        if f.startswith("p_"):
            return "own price history"
        if f.startswith(("w_", "wz_", "weather")):
            return "weather"
        if f.startswith("cal_"):
            return "calendar"
        if f.startswith("nb_"):
            return "neighbour zones"
        return "other"

    g = fi.assign(g=fi.feature.map(group)).groupby("g")["gain_share"].sum().sort_values()
    top = fi.nlargest(12, "gain_share").iloc[::-1]
    fig, axes = plt.subplots(1, 2, figsize=(7, 2.8), gridspec_kw={"width_ratios": [1, 1.4]})
    axes[0].barh(g.index, g.values * 100, color="#1f77b4")
    axes[0].set_xlabel("share of gain (%)")
    axes[0].set_title("feature groups")
    axes[1].barh(top.feature, top.gain_share * 100, color="#6baed6")
    axes[1].set_xlabel("share of gain (%)")
    axes[1].set_title("top 12 features")
    axes[1].tick_params(axis="y", labelsize=6.5)
    save(fig, "importance")


def fig_hpo() -> None:
    fig, axes = plt.subplots(1, 4, figsize=(7, 2.1), sharey=False)
    for ax, s in zip(
        axes,
        sorted(optuna.get_all_study_summaries("sqlite:///optuna.db"), key=lambda s: s.study_name),
        strict=False,
    ):
        st = optuna.load_study(study_name=s.study_name, storage="sqlite:///optuna.db")
        done = [t for t in st.trials if t.value is not None]
        vals = [t.value for t in done]
        ax.scatter([t.number for t in done], vals, s=6, color="#9e9e9e")
        ax.plot([t.number for t in done], np.minimum.accumulate(vals), color="#d62728", lw=1)
        ax.set_title(s.study_name.split("-")[0])
        ax.set_xlabel("trial")
    axes[0].set_ylabel("CV pinball (q10/50/90)")
    save(fig, "hpo")


def fig_weights() -> None:
    fig, ax = plt.subplots(figsize=(4.6, 2.8))
    for z, c in zip(ZONES, ["#6baed6", "#3182bd", "#08519c", "#e6550d"], strict=True):
        w = artifact(MODELS["Ensemble (deployable)"].format(z=z), "weight_log.csv").iloc[-1]
        ax.plot(
            range(24),
            [w[f"w_{h}_timesfm25"] for h in range(24)],
            marker="o",
            ms=2,
            lw=1,
            color=c,
            label=z,
        )
    ax.set_xlabel("hour of delivery day (local)")
    ax.set_ylabel("weight on TimesFM 2.5")
    ax.set_xticks(range(0, 24, 3))
    ax.set_ylim(0, 1)
    ax.legend(frameon=False, ncol=2)
    save(fig, "weights")


if __name__ == "__main__":
    fig_prices()
    fig_weather_gap()
    t = fig_zone_scores()
    print(t.pivot(index="model", columns="zone", values="mean").round(3))
    fig_hourly()
    fig_reliability()
    fig_fan()
    fig_importance()
    fig_hpo()
    fig_weights()
