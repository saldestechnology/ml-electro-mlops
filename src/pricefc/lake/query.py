"""DuckDB views over silver Parquet tables."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pricefc.config import BaseConfig

if TYPE_CHECKING:
    import duckdb


def _silver_root(base: BaseConfig) -> Path:
    return base.paths.data_root / "lake" / "silver"


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def table_metadata(base: BaseConfig) -> list[dict[str, Any]]:
    """Return the silver table entries from the build manifest."""
    manifest_path = _silver_root(base) / "_manifest.json"
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text())
    return sorted(manifest.get("tables", {}).values(), key=lambda item: item["table_name"])


def connect(base: BaseConfig) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB connection with one view per silver table.

    DuckDB is imported here, not at package import time, so ingestion remains usable in
    environments where the optional runtime module has not yet been installed.
    """
    import duckdb

    connection = duckdb.connect(database=":memory:")
    silver_root = _silver_root(base)
    entries = table_metadata(base)
    price_views: list[tuple[str, str, str]] = []
    for entry in entries:
        table_name = str(entry["table_name"])
        table_path = silver_root / str(entry["source"]) / str(entry["dataset"]) / str(entry["key"])
        parts = sorted(table_path.glob("year=*/part-0.parquet"))
        empty_file = table_path / "_empty.parquet"
        data_files = parts or ([empty_file] if empty_file.exists() else [])
        if data_files:
            file_list = "[" + ", ".join(_quote_literal(p.as_posix()) for p in data_files) + "]"
            connection.execute(
                f"CREATE VIEW {_quote_identifier(table_name)} AS "
                f"SELECT * FROM read_parquet({file_list}, hive_partitioning={bool(parts)})"
            )
            if entry["dataset"] == "day_ahead_prices":
                price_views.append((table_name, str(entry["key"]), str(entry["source"])))

        conflicts = table_path / "_conflicts.parquet"
        if conflicts.exists():
            conflict_view = f"{table_name}_conflicts"
            connection.execute(
                f"CREATE VIEW {_quote_identifier(conflict_view)} AS "
                f"SELECT * FROM read_parquet({_quote_literal(conflicts.as_posix())})"
            )

    if price_views:
        selects = [
            f"SELECT *, {_quote_literal(zone)} AS zone, {_quote_literal(source)} AS source "
            f"FROM {_quote_identifier(name)}"
            for name, zone, source in sorted(price_views, key=lambda row: (row[1], row[2]))
        ]
        connection.execute(f"CREATE VIEW prices AS {' UNION ALL BY NAME '.join(selects)}")
    return connection


def execute_readonly(connection: duckdb.DuckDBPyConnection, sql: str) -> duckdb.DuckDBPyConnection:
    """Execute one SELECT statement; reject DDL, writes, and multi-statement SQL."""
    statements = connection.extract_statements(sql)
    if len(statements) != 1 or str(statements[0].type).upper() not in {
        "SELECT",
        "STATEMENTTYPE.SELECT",
    }:
        raise ValueError("lake sql accepts exactly one read-only SELECT statement")
    return connection.execute(sql)
