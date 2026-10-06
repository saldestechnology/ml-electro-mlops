"""Command-line entrypoint: `python -m pricefc ...`."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import typer
import yaml
from dotenv import load_dotenv

from pricefc.config import BaseConfig, config_hash, load_config

if TYPE_CHECKING:
    from pricefc.datasets.loaders import WeatherKind
    from pricefc.ingest.run import IngestResult

app = typer.Typer(no_args_is_help=True, add_completion=False)

CONFIG_OPT = typer.Option(Path("configs/base.yaml"), "--config", "-c", help="Base config file.")


@app.callback()
def _main() -> None:
    load_dotenv()


@app.command()
def show_config(config: Path = CONFIG_OPT) -> None:
    """Print the resolved config and its hash."""
    cfg = load_config(config)
    typer.echo(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False))
    typer.echo(f"config_hash: {config_hash(cfg)}")


@app.command()
def init_tracking(
    config: Path = CONFIG_OPT,
    tracking_uri: str | None = typer.Option(None, help="Overrides config mlflow.tracking_uri."),
) -> None:
    """Create the standard MLflow experiments."""
    from pricefc.tracking.mlflow_utils import EXPERIMENTS, setup_tracking

    setup_tracking(load_config(config), tracking_uri)
    typer.echo(f"experiments ready: {', '.join(EXPERIMENTS)}")


ingest_app = typer.Typer(no_args_is_help=True, help="Pull raw data into immutable snapshots.")
app.add_typer(ingest_app, name="ingest")

INGEST_OPT = typer.Option(Path("configs/ingest.yaml"), "--ingest-config")
FEATURES_OPT = typer.Option(Path("configs/features.yaml"), "--features-config")
DATASET_OPT = typer.Option(None, "--dataset", "-d", help="Repeatable.")
PRICE_AREA_OPT = typer.Option(
    None, "--price-area", help="Pull day-ahead prices for these areas only (e.g. NO1)."
)
ZONE_OPT = typer.Option(None, "--zone", "-z", help="Repeatable. Default: zones in base config.")
START_OPT = typer.Option(None, formats=["%Y-%m-%d"], help="UTC start date.")
END_OPT = typer.Option(None, formats=["%Y-%m-%d"], help="UTC end date.")


def _date(d: datetime | None) -> date | None:
    return d.date() if d else None


def _finish(
    results: list[IngestResult],
    label: str,
    zones: list[str],
    base: BaseConfig,
    weather_source: str,
) -> None:
    from pricefc.ingest.run import log_ingest_run

    run_id = log_ingest_run(base, results, zones=zones, label=label, weather_source=weather_source)
    failed = [r for r in results if not r.report.passed]
    for r in results:
        status = "ok" if r.report.passed else "FAILED"
        typer.echo(f"[{status}] {r.snapshot.path} rows={r.snapshot.manifest['row_count']}")
    typer.echo(f"mlflow ingest run: {run_id}")
    if failed:
        typer.echo(f"{len(failed)} snapshot(s) failed validation; see MLflow run.", err=True)
        raise typer.Exit(1)


@ingest_app.command("weather")
def ingest_weather_cmd(
    endpoint: str = typer.Argument(
        ..., help="historical_forecast | previous_runs | single_runs | forecast"
    ),
    zone: list[str] | None = ZONE_OPT,
    start: datetime | None = START_OPT,
    end: datetime | None = END_OPT,
    config: Path = CONFIG_OPT,
    ingest_config: Path = INGEST_OPT,
    features_config: Path = FEATURES_OPT,
) -> None:
    """Pull Open-Meteo weather for every configured location of the zone(s)."""
    from pricefc.config import load_features_config, load_ingest_config
    from pricefc.ingest.openmeteo import ENDPOINTS, weather_source_tag
    from pricefc.ingest.run import ingest_weather, locations_for

    if endpoint not in ENDPOINTS:
        raise typer.BadParameter(f"endpoint must be one of {ENDPOINTS}")
    base = load_config(config)
    ing = load_ingest_config(ingest_config)
    zones = zone or list(base.zones)
    locs = locations_for(load_features_config(features_config), zones)
    results = ingest_weather(base, ing, locs, endpoint, _date(start), _date(end))
    _finish(
        results,
        f"weather-{endpoint}",
        zones,
        base,
        weather_source_tag(endpoint, ing.open_meteo.model),
    )


@ingest_app.command("entsoe")
def ingest_entsoe_cmd(
    zone: list[str] | None = ZONE_OPT,
    dataset: list[str] | None = DATASET_OPT,
    price_area: list[str] | None = PRICE_AREA_OPT,
    start: datetime | None = START_OPT,
    end: datetime | None = END_OPT,
    config: Path = CONFIG_OPT,
    ingest_config: Path = INGEST_OPT,
) -> None:
    """Pull ENTSO-E datasets for the zone(s). Requires ENTSOE_API_TOKEN."""
    from pricefc.config import load_ingest_config
    from pricefc.ingest.entsoe import jobs_for_zone, price_job
    from pricefc.ingest.run import ingest_entsoe

    base = load_config(config)
    ing = load_ingest_config(ingest_config)
    zones = zone or list(base.zones)
    if price_area:
        jobs = [price_job(a) for a in price_area]
    else:
        jobs = [j for z in zones for j in jobs_for_zone(z, ing.entsoe)]
        if dataset:
            jobs = [j for j in jobs if j.dataset in dataset]
    # Neighbouring zones share exchange series and the hydro area; pull each once.
    jobs = list({(j.dataset, j.key): j for j in jobs}.values())
    results = ingest_entsoe(base, ing, jobs, _date(start), _date(end))
    _finish(results, "entsoe", zones, base, "none")


@ingest_app.command("prices")
def ingest_prices_cmd(
    zone: list[str] | None = ZONE_OPT,
    start: datetime | None = START_OPT,
    end: datetime | None = END_OPT,
    source: str | None = typer.Option(None, help="Override prices.source from ingest config."),
    force: bool = typer.Option(False, help="Re-pull months that already have a snapshot."),
    config: Path = CONFIG_OPT,
    ingest_config: Path = INGEST_OPT,
) -> None:
    """Pull day-ahead prices (local delivery days) per zone and month. Resumable."""
    from zoneinfo import ZoneInfo

    from pricefc.config import load_ingest_config
    from pricefc.ingest.prices import ElprisSource, EntsoePriceSource, PriceSource
    from pricefc.ingest.run import ingest_prices, make_entsoe_client

    base = load_config(config)
    ing = load_ingest_config(ingest_config)
    zones = zone or list(base.zones)
    name = source or ing.prices.source
    src: PriceSource
    if name == "elprisetjustnu":
        src = ElprisSource(ing.elprisetjustnu)
        default_first = ing.elprisetjustnu.start
    elif name == "entsoe":
        src = EntsoePriceSource(ing.entsoe, make_entsoe_client())
        default_first = ing.entsoe.history_start
    else:
        raise typer.BadParameter(f"unknown price source {name!r}")
    today = datetime.now(ZoneInfo(base.timezone)).date()
    first = _date(start) or default_first
    last = _date(end) or today
    results = ingest_prices(base, src, zones, first, last, force=force)
    if not results:
        typer.echo("nothing to do: all months already stored")
        return
    _finish(results, f"prices-{name}", zones, base, "none")


FIRST_ORIGIN_OPT = typer.Option(None, formats=["%Y-%m-%d"], help="First origin day.")
LAST_ORIGIN_OPT = typer.Option(None, formats=["%Y-%m-%d"], help="Last origin day.")
dataset_app = typer.Typer(no_args_is_help=True, help="Build versioned datasets.")
app.add_typer(dataset_app, name="dataset")


@dataset_app.command("build")
def dataset_build_cmd(
    weather: str = typer.Argument(..., help="stitched (training) | true_lead (backtests)"),
    zone: list[str] | None = ZONE_OPT,
    start: datetime | None = FIRST_ORIGIN_OPT,
    end: datetime | None = LAST_ORIGIN_OPT,
    config: Path = CONFIG_OPT,
    ingest_config: Path = INGEST_OPT,
    features_config: Path = FEATURES_OPT,
) -> None:
    """Build, leakage-audit, store and log a dataset per zone."""
    from pricefc.config import load_features_config, load_ingest_config
    from pricefc.datasets.build import build_dataset

    if weather not in ("stitched", "true_lead"):
        raise typer.BadParameter("weather must be stitched or true_lead")
    base = load_config(config)
    feats = load_features_config(features_config)
    ing = load_ingest_config(ingest_config)
    for z in zone or list(base.zones):
        stored, run_id = build_dataset(
            base, feats, ing, z, cast("WeatherKind", weather), _date(start), _date(end)
        )
        m = stored.manifest
        typer.echo(
            f"{z}: {stored.path} rows={m['rows']} features={len(m['feature_columns'])} "
            f"origins={m['origin_first']}..{m['origin_last']} "
            f"leakage_audit=passed({len(m['leakage_audit']['origins_checked'])} origins) "
            f"mlflow={run_id}"
        )


backtest_app = typer.Typer(no_args_is_help=True, help="Rolling-origin backtests.")
app.add_typer(backtest_app, name="backtest")
BACKTEST_OPT = typer.Option(Path("configs/backtest.yaml"), "--backtest-config")
MODELS_DIR_OPT = typer.Option(Path("configs/models"), "--models-dir")
MODEL_OPT = typer.Option(None, "--model", "-m", help="Repeatable. Default: backtest config.")
DEV_OPT = typer.Option(False, "--dev", help="Every n-th origin only (fast).")
SEEDS_OPT = typer.Option(None, "--seed", help="Repeatable: run every model once per seed.")
VERSION_OPT = typer.Option(None, help="Dataset version (default: latest).")


@backtest_app.command("run")
def backtest_run_cmd(
    zone: list[str] | None = ZONE_OPT,
    model: list[str] | None = MODEL_OPT,
    dev: bool = DEV_OPT,
    seeds: list[int] | None = SEEDS_OPT,
    train_version: str | None = VERSION_OPT,
    eval_version: str | None = VERSION_OPT,
    config: Path = CONFIG_OPT,
    backtest_config: Path = BACKTEST_OPT,
    models_dir: Path = MODELS_DIR_OPT,
) -> None:
    """Backtest models on the true-lead dataset and compare them (bootstrap + DM)."""
    import pandas as pd

    from pricefc.backtest.run import run_zone
    from pricefc.config import load_backtest_config, load_model_params

    base = load_config(config)
    cfg = load_backtest_config(backtest_config)
    params = load_model_params(models_dir)
    for z in zone or list(base.zones):
        specs = list(model or cfg.models)
        if seeds:
            specs = [f"{m}@seed={s}" for m in specs for s in seeds]
        _, table = run_zone(
            base,
            cfg,
            params,
            z,
            models=specs,
            dev=dev,
            train_version=train_version,
            eval_version=eval_version,
        )
        cols = [
            "model",
            "days",
            "pinball_mean",
            "pinball_lo",
            "pinball_hi",
            "ae_mean",
            "pinball_skill_vs_ref",
            "pinball_diff_vs_ref",
            "pinball_diff_lo",
            "pinball_diff_hi",
            "pinball_dm_p",
        ]
        with pd.option_context("display.width", 200, "display.float_format", "{:.3f}".format):
            typer.echo(f"\n{z} ({'dev' if dev else 'full'}), reference={cfg.reference_model}")
            typer.echo(table[[c for c in cols if c in table.columns]].to_string(index=False))


REFERENCE_OPT = typer.Option(None, help="Model spec to compare against (default: config).")
RUN_IDS_ARG = typer.Argument(..., help="MLflow backtest run IDs (same zone).")


@backtest_app.command("compare")
def backtest_compare_cmd(
    run_id: list[str] = RUN_IDS_ARG,
    reference: str | None = REFERENCE_OPT,
    config: Path = CONFIG_OPT,
    backtest_config: Path = BACKTEST_OPT,
) -> None:
    """Compare logged backtest runs (bootstrap CIs + Diebold-Mariano) and log the result."""
    import pandas as pd

    from pricefc.backtest.run import compare_runs
    from pricefc.config import load_backtest_config

    cfg = load_backtest_config(backtest_config)
    if reference:
        cfg = cfg.model_copy(update={"reference_model": reference})
    table = compare_runs(load_config(config), cfg, run_id)
    cols = [
        "model",
        "days",
        "pinball_mean",
        "pinball_lo",
        "pinball_hi",
        "ae_mean",
        "pinball_skill_vs_ref",
        "pinball_diff_vs_ref",
        "pinball_diff_lo",
        "pinball_diff_hi",
        "pinball_dm_p",
    ]
    with pd.option_context("display.width", 200, "display.float_format", "{:.3f}".format):
        typer.echo(table[[c for c in cols if c in table.columns]].to_string(index=False))


HPO_OPT = typer.Option(Path("configs/hpo/lightgbm.yaml"), "--hpo-config")


@app.command("tune")
def tune_cmd(
    zone: list[str] | None = ZONE_OPT,
    train_version: str | None = VERSION_OPT,
    config: Path = CONFIG_OPT,
    hpo_config: Path = HPO_OPT,
) -> None:
    """Tune LightGBM with Optuna on data before the evaluation window; write the config."""
    from datetime import date as date_

    from pricefc.backtest.run import latest_dataset
    from pricefc.models.tuning import load_hpo_config, tune_zone

    base = load_config(config)
    cfg = load_hpo_config(hpo_config)
    for z in zone or list(base.zones):
        eval_ds = latest_dataset(base, z, "true_lead")
        eval_start = date_.fromisoformat(eval_ds.manifest["origin_first"])
        train_ds = latest_dataset(base, z, "stitched", train_version)
        study, run_id, path = tune_zone(base, cfg, z, eval_start, train_ds)
        typer.echo(f"{z}: best pinball {study.best_value:.4f} params {study.best_params}")
        typer.echo(f"{z}: wrote {path} (mlflow run {run_id})")


state_app = typer.Typer(no_args_is_help=True, help="Served model state between origins.")
app.add_typer(state_app, name="state")
STATE_DIR_OPT = typer.Option(None, "--dir", help="Default: <data_root>/state/<zone>/<model>.")
UNTIL_OPT = typer.Option(None, formats=["%Y-%m-%d"], help="Default: latest origin in dataset.")
FIRST_OPT = typer.Option(
    None, formats=["%Y-%m-%d"], help="Fresh state only. Default: dataset start."
)
OUT_OPT = typer.Option(None, "--forecasts-out", help="Write the new forecasts (Parquet).")


@state_app.command("advance")
def state_advance_cmd(
    zone: list[str] | None = ZONE_OPT,
    model: str = typer.Option("ensemble_hourly_exp", "--model", "-m"),
    until: datetime | None = UNTIL_OPT,
    first_origin: datetime | None = FIRST_OPT,
    state_dir: Path | None = STATE_DIR_OPT,
    forecasts_out: Path | None = OUT_OPT,
    train_version: str | None = VERSION_OPT,
    eval_version: str | None = VERSION_OPT,
    config: Path = CONFIG_OPT,
    models_dir: Path = MODELS_DIR_OPT,
) -> None:
    """Load (or start) a model's state and step it through every pending origin up to
    `--until`, exactly as the backtest would; save it after."""
    from datetime import date as date_

    from pricefc.backtest.run import latest_dataset
    from pricefc.config import load_model_params
    from pricefc.serving.state import META_FILE, ModelState, new_state

    base = load_config(config)
    for z in zone or list(base.zones):
        d = state_dir or base.paths.data_root / "state" / z / model
        eval_ds = latest_dataset(base, z, "true_lead", eval_version)
        train_ds = latest_dataset(base, z, "stitched", train_version)
        train_df, eval_df = train_ds.read(), eval_ds.read()
        if (d / META_FILE).exists():
            state = ModelState.load(d)
            if state.zone != z or state.spec != model:
                raise typer.BadParameter(f"{d} holds {state.zone}/{state.spec}")
        else:
            first = _date(first_origin) or date_.fromisoformat(eval_ds.manifest["origin_first"])
            state = new_state(model, z, base, load_model_params(models_dir), first_origin=first)
        end = _date(until) or max(date_.fromisoformat(o) for o in eval_df["origin_date"].unique())
        fc = state.advance(train_df, eval_df, end)
        state.datasets = {"train": train_ds.version, "eval": eval_ds.version}
        state.save(d)
        if forecasts_out:
            out = forecasts_out if len(zone or base.zones) == 1 else forecasts_out / f"{z}.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            fc.to_parquet(out, index=False)
        typer.echo(
            f"{z}: {fc['origin_date'].nunique()} origin(s) {fc['origin_date'].min()}.."
            f"{fc['origin_date'].max()}, last fit {state.last_fit}, saved {d}"
        )


ALIAS_OPT = typer.Option("champion", help="Registry alias.")


@state_app.command("register")
def state_register_cmd(
    zone: list[str] | None = ZONE_OPT,
    model: str = typer.Option("ensemble_hourly_exp", "--model", "-m"),
    state_dir: Path | None = STATE_DIR_OPT,
    alias: str | None = typer.Option(None, help="Point this alias at the new version."),
    description: str | None = typer.Option(None),
    config: Path = CONFIG_OPT,
) -> None:
    """Register a saved state as a new version of se-price-<zone>-hourly."""
    from pricefc.serving.registry import model_name, register_state
    from pricefc.serving.state import ModelState

    base = load_config(config)
    for z in zone or list(base.zones):
        d = state_dir or base.paths.data_root / "state" / z / model
        state = ModelState.load(d)
        version = register_state(base, state, d, alias=alias, description=description)
        typer.echo(
            f"{z}: {model_name(z)} version {version} (last origin {state.last_origin})"
            + (f", alias {alias}" if alias else "")
        )


@state_app.command("pull")
def state_pull_cmd(
    zone: list[str] | None = ZONE_OPT,
    alias: str = ALIAS_OPT,
    model: str = typer.Option("ensemble_hourly_exp", "--model", "-m"),
    state_dir: Path | None = STATE_DIR_OPT,
    config: Path = CONFIG_OPT,
) -> None:
    """Replace the local state with the registry version an alias points at."""
    from pricefc.serving.registry import pull_state, resolve
    from pricefc.tracking.mlflow_utils import setup_tracking

    base = load_config(config)
    setup_tracking(base)
    for z in zone or list(base.zones):
        d = state_dir or base.paths.data_root / "state" / z / model
        version = resolve(z, alias)
        state = pull_state(z, version, d)
        typer.echo(f"{z}: pulled version {version} ({state.spec}, last origin {state.last_origin})")


@app.command()
def serve() -> None:
    """Run the scheduled Prefect deployments (VPS worker; needs the `serve` extra)."""
    from pricefc.flows.daily import serve as serve_flows

    serve_flows()


@app.command()
def snapshots(config: Path = CONFIG_OPT) -> None:
    """List raw snapshots with row counts and validation status."""
    import json

    root = load_config(config).paths.raw
    for manifest in sorted(root.glob("*/*/*/pulled_at=*/manifest.json")):
        m = json.loads(manifest.read_text())
        status = "ok" if m["validation"]["passed"] else "FAILED"
        typer.echo(
            f"[{status}] {m['source']}/{m['dataset']}/{m['key']} "
            f"pulled={m['pulled_at']} rows={m['row_count']} "
            f"{m['data_start']} .. {m['data_end']}"
        )


if __name__ == "__main__":
    app()
