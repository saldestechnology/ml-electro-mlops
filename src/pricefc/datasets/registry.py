"""Versioned dataset storage and MLflow lineage (spec section 6.3).

Layout: {datasets_root}/{zone}/{resolution}/{dataset_version}/data.parquet + manifest.json,
where dataset_version = YYYYMMDD-<first 6 hex of the content digest>.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from pricefc.config import BaseConfig, FeaturesConfig, config_hash
from pricefc.ingest.snapshot import data_hash
from pricefc.lineage import git_info


@dataclass(frozen=True)
class StoredDataset:
    path: Path
    version: str
    manifest: dict[str, Any]

    @property
    def data_file(self) -> Path:
        return self.path / "data.parquet"

    def read(self) -> pd.DataFrame:
        return pd.read_parquet(self.data_file)


def dataset_version(digest: str, built: datetime) -> str:
    return f"{built:%Y%m%d}-{digest[:6]}"


def write_dataset(
    df: pd.DataFrame,
    *,
    base: BaseConfig,
    features: FeaturesConfig,
    zone: str,
    name: str,
    description: dict[str, Any],
    build_info: dict[str, Any],
    leakage_report: dict[str, Any],
    built: datetime | None = None,
) -> StoredDataset:
    """Write a dataset once. Rebuilding identical content on the same day reuses it."""
    built = built or datetime.now(UTC)
    digest = data_hash(df)
    version = dataset_version(digest, built)
    path = base.paths.datasets / zone / base.resolution / version
    if (path / "manifest.json").exists():
        manifest = json.loads((path / "manifest.json").read_text())
        if manifest["data_sha256"] != digest:
            raise RuntimeError(f"{path} exists with different content")
        return StoredDataset(path, version, manifest)

    path.mkdir(parents=True)
    df.to_parquet(path / "data.parquet", index=False)
    sha, dirty = git_info()
    feature_cols = [c for c in df.columns if c not in build_info["meta_columns"] + ["y"]]
    manifest = {
        "name": name,
        "dataset_version": version,
        "zone": zone,
        "resolution": base.resolution,
        "built_at": built.isoformat(),
        "data_sha256": digest,
        "rows": len(df),
        "origin_first": str(df["origin_date"].min()),
        "origin_last": str(df["origin_date"].max()),
        "target": "y",
        "meta_columns": build_info["meta_columns"],
        "feature_columns": feature_cols,
        "feature_config_hash": config_hash(features),
        "base_config_hash": config_hash(base),
        "git_sha": sha,
        "git_dirty": dirty,
        "build": {k: v for k, v in build_info.items() if k != "meta_columns"},
        "leakage_audit": leakage_report,
        **description,
    }
    (path / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    for f in path.iterdir():
        os.chmod(f, 0o444)
    return StoredDataset(path, version, manifest)


def find_datasets(base: BaseConfig, zone: str) -> list[StoredDataset]:
    root = base.paths.datasets / zone / base.resolution
    out = []
    for m in sorted(root.glob("*/manifest.json")):
        manifest = json.loads(m.read_text())
        out.append(StoredDataset(m.parent, manifest["dataset_version"], manifest))
    return out


def log_dataset_run(base: BaseConfig, ds: StoredDataset, df: pd.DataFrame) -> str:
    """MLflow run in the `datasets` experiment with the dataset as a tracked input."""
    import mlflow
    from mlflow.data.pandas_dataset import from_pandas

    from pricefc.tracking.mlflow_utils import base_tags, setup_tracking, start_run

    m = ds.manifest
    setup_tracking(base)
    tags = base_tags(
        base,
        zone=m["zone"],
        pipeline_stage="build_dataset",
        weather_source=m["weather_source"],
        dataset_version=ds.version,
    )
    with start_run("datasets", tags, base, run_name=f"{m['name']}-{ds.version}") as run:
        mlflow.log_params(
            {
                "rows": m["rows"],
                "n_features": len(m["feature_columns"]),
                "origin_first": m["origin_first"],
                "origin_last": m["origin_last"],
                "weather_kind": m["weather_kind"],
                "feature_config_hash": m["feature_config_hash"],
                "data_sha256": m["data_sha256"],
                "rows_dropped_no_target": m["build"]["rows_dropped_no_target"],
            }
        )
        dataset = from_pandas(
            df, source=ds.data_file.resolve().as_uri(), name=m["name"], targets="y"
        )
        mlflow.log_input(dataset, context="dataset")
        mlflow.log_dict(m, "manifest.json")
        mlflow.log_dict(m["sources"], "raw_snapshots.json")
        mlflow.log_dict(m["leakage_audit"], "leakage_audit.json")
        return str(run.info.run_id)
