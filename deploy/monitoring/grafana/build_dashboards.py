"""Generate the provisioned Grafana dashboards (grafana/dashboards/*.json).

    python deploy/monitoring/grafana/build_dashboards.py

The JSON files are committed; edit this script, not the JSON (Grafana's UI edits are disabled).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).parent / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}
ZONES = ["SE1", "SE2", "SE3", "SE4"]


class Grid:
    """Left-to-right panel placement on Grafana's 24-column grid."""

    def __init__(self) -> None:
        self.x = self.y = self.row_h = 0

    def place(self, w: int, h: int) -> dict[str, int]:
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def newline(self) -> None:
        if self.x:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0


def target(expr: str, legend: str = "", instant: bool = False) -> dict[str, Any]:
    return {
        "datasource": DS,
        "expr": expr,
        "legendFormat": legend or "__auto",
        "instant": instant,
        "range": not instant,
        "refId": "A",
    }


def with_refs(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for i, t in enumerate(targets):
        t["refId"] = chr(ord("A") + i)
    return targets


def panel(kind: str, title: str, targets: list[dict[str, Any]], grid: Grid, w: int, h: int,
          unit: str = "short", desc: str = "", **opts: Any) -> dict[str, Any]:
    p: dict[str, Any] = {
        "type": kind,
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": grid.place(w, h),
        "targets": with_refs(targets),
        "fieldConfig": {"defaults": {"unit": unit, **opts.pop("defaults", {})}, "overrides": opts.pop("overrides", [])},
        "options": opts.pop("options", {}),
    }
    p.update(opts)
    return p


def row(title: str, grid: Grid) -> dict[str, Any]:
    grid.newline()
    p = {"type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": grid.y, "w": 24, "h": 1}, "panels": []}
    grid.y += 1
    return p


def thresholds(*steps: tuple[float | None, str]) -> dict[str, Any]:
    return {"thresholds": {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in steps]}}


def dashboard(uid: str, title: str, panels: list[dict[str, Any]], variables: list[dict[str, Any]] | None = None,
              time_from: str = "now-7d") -> dict[str, Any]:
    for i, p in enumerate(panels, 1):
        p["id"] = i
    return {
        "uid": uid,
        "title": title,
        "tags": ["pricefc"],
        "timezone": "Europe/Stockholm",
        "schemaVersion": 41,
        "editable": False,
        "refresh": "1m",
        "time": {"from": time_from, "to": "now"},
        "templating": {"list": variables or []},
        "panels": panels,
    }


def env_var() -> dict[str, Any]:
    return {
        "name": "env",
        "type": "query",
        "datasource": DS,
        "query": {"query": "label_values(pricefc_build_info, env)", "refId": "env"},
        "refresh": 2,
        "current": {"text": "staging", "value": "staging"},
    }


def operations() -> dict[str, Any]:
    g = Grid()
    age = 'time() - {}'
    ps: list[dict[str, Any]] = [row("Now", g)]
    ps.append(panel(
        "stat", "Firing alerts", [target('count(ALERTS{alertstate="firing", severity!="none"}) or vector(0)', instant=True)],
        g, 4, 4, defaults=thresholds((None, "green"), (1, "red")), options={"colorMode": "background", "graphMode": "none"}))
    ps.append(panel(
        "stat", "Champion forecast for", [target('pricefc_forecast_target_date_timestamp_seconds{env="$env", role="champion"} * 1000', "{{zone}}", True)],
        g, 8, 4, unit="dateTimeAsLocalNoDateIfToday",
        desc="Delivery day of the newest champion forecast per zone. After 09:30 this should be tomorrow.",
        options={"colorMode": "none", "graphMode": "none", "textMode": "value_and_name"}))
    ps.append(panel(
        "stat", "Scheduled runs (next 48 h)", [target('sum(pricefc_flow_runs_scheduled{env="$env"})', instant=True)],
        g, 4, 4, desc="0 means Prefect's scheduler is not creating runs (2026-10-07 incident).",
        defaults=thresholds((None, "red"), (1, "green")), options={"colorMode": "background", "graphMode": "none"}))
    ps.append(panel(
        "stat", "Last backup", [target('(time() - pricefc_backup_last_success_timestamp_seconds{mode="backup"})', instant=True)],
        g, 4, 4, unit="s", defaults=thresholds((None, "green"), (30 * 3600, "orange"), (36 * 3600, "red")),
        options={"colorMode": "background", "graphMode": "none"}))
    ps.append(panel(
        "stat", "Services", [target('min(up)', instant=True)], g, 4, 4,
        desc="1 if every scrape target (incl. the tunnel to the main VPS) is up.",
        defaults={**thresholds((None, "red"), (1, "green")), "mappings": [{"type": "value", "options": {"0": {"text": "DOWN"}, "1": {"text": "UP"}}}]},
        options={"colorMode": "background", "graphMode": "none"}))
    ps.append(panel(
        "alertlist", "Alerts", [], g, 24, 6,
        options={"alertInstanceLabelFilter": "", "datasource": "Alertmanager", "groupMode": "default", "stateFilter": {"firing": True, "pending": True}, "viewMode": "list"},
        datasource={"type": "alertmanager", "uid": "alertmanager"}))

    ps.append(row("Pipeline", g))
    ps.append(panel(
        "table", "Flows", [
            target(f'{age.format("pricefc_flow_last_success_timestamp_seconds")}{{env="$env"}}', instant=True),
            target('pricefc_flow_last_success_duration_seconds{env="$env"}', instant=True),
            target('pricefc_flow_runs_scheduled{env="$env"}', instant=True),
        ], g, 12, 6, transformations=[
            {"id": "merge"},
            {"id": "organize", "options": {"excludeByName": {"Time": True, "__name__": True, "env": True, "host": True, "instance": True, "job": True},
                                           "renameByName": {"Value #A": "since last success", "Value #B": "duration", "Value #C": "scheduled 48 h"}}},
        ], overrides=[
            {"matcher": {"id": "byName", "options": "since last success"}, "properties": [{"id": "unit", "value": "s"}]},
            {"matcher": {"id": "byName", "options": "duration"}, "properties": [{"id": "unit", "value": "s"}]},
        ]))
    ps.append(panel(
        "table", "Raw data freshness", [
            target(f'{age.format("pricefc_raw_latest_valid_pulled_at_timestamp_seconds")}{{env="$env"}}', instant=True),
            target('pricefc_raw_latest_pull_valid{env="$env"}', instant=True),
        ], g, 12, 6, unit="s", transformations=[
            {"id": "merge"},
            {"id": "groupBy", "options": {"fields": {"source": {"operation": "groupby", "aggregations": []}, "dataset": {"operation": "groupby", "aggregations": []},
                                                     "Value #A": {"operation": "aggregate", "aggregations": ["max"]}, "Value #B": {"operation": "aggregate", "aggregations": ["min"]}}}},
            {"id": "organize", "options": {"renameByName": {"Value #A (max)": "oldest key's last valid pull", "Value #B (min)": "all newest pulls valid"}}},
        ], overrides=[{"matcher": {"id": "byName", "options": "all newest pulls valid"}, "properties": [{"id": "unit", "value": "bool_yes_no"}]}]))
    ps.append(panel(
        "state-timeline", "Flow run state", [target('max by (flow, state) (pricefc_flow_last_run_state{env="$env"}) == 1', "{{flow}} {{state}}")],
        g, 24, 5, options={"showValue": "never", "mergeValues": True}))

    ps.append(row("Hosts and containers", g))
    ps.append(panel(
        "timeseries", "Container memory (main VPS)", [target('pricefc_container_memory_bytes{env="$env"}', "{{name}}")],
        g, 12, 7, unit="bytes", desc="Worker peaks during forecast-daily (both models): ~3 GB on 2026-10-07.",
        options={"tooltip": {"mode": "multi"}, "legend": {"displayMode": "table", "placement": "right", "calcs": ["max"]}}))
    ps.append(panel(
        "timeseries", "Memory available", [target('node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes', "{{host}}")],
        g, 6, 7, unit="percentunit", defaults={"min": 0, "max": 1}))
    ps.append(panel(
        "timeseries", "Disk free (/)", [target('node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}', "{{host}}")],
        g, 6, 7, unit="percentunit", defaults={"min": 0, "max": 1}))
    ps.append(panel(
        "timeseries", "CPU busy", [target('1 - avg by (host) (rate(node_cpu_seconds_total{mode="idle"}[5m]))', "{{host}}")],
        g, 12, 6, unit="percentunit", defaults={"min": 0, "max": 1}))
    ps.append(panel(
        "table", "Containers", [
            target('pricefc_container_running{env="$env"}', instant=True),
            target('pricefc_container_healthy{env="$env"}', instant=True),
            target('pricefc_container_restarts{env="$env"}', instant=True),
        ], g, 12, 6, transformations=[
            {"id": "merge"},
            {"id": "organize", "options": {"excludeByName": {"Time": True, "__name__": True, "env": True, "host": True, "instance": True, "job": True},
                                           "renameByName": {"Value #A": "running", "Value #B": "healthy", "Value #C": "restarts"}}},
        ]))
    return dashboard("pricefc-ops", "pricefc · Operations", ps, [env_var()], "now-2d")


def quality() -> dict[str, Any]:
    g = Grid()
    ps: list[dict[str, Any]] = []
    zone_var = {
        "name": "zone", "type": "custom", "query": ",".join(ZONES), "multi": True, "includeAll": True,
        "current": {"text": "All", "value": "$__all"},
        "options": [],
    }
    window_var = {"name": "window", "type": "custom", "query": "14d,7d", "current": {"text": "14d", "value": "14d"}, "options": []}
    ps.append(row("Champion vs challenger (rolling $window)", g))
    ps.append(panel(
        "bargauge", "Pinball: challenger vs champion",
        [target('pricefc_pinball_mean{env="$env", zone=~"$zone", role="challenger", window="$window"} / on(env, zone) '
                'pricefc_pinball_mean{env="$env", zone=~"$zone", role="champion", window="$window"} - 1', "{{zone}}", True)],
        g, 8, 7, unit="percentunit", desc="Negative = challenger better. The backtest said −7 % to −15 %.",
        defaults={**thresholds((None, "green"), (0, "red")), "min": -0.5, "max": 0.5},
        options={"orientation": "horizontal", "displayMode": "basic"}))
    ps.append(panel(
        "table", "Scores", [
            target('pricefc_pinball_mean{env="$env", zone=~"$zone", window="$window"}', instant=True),
            target('1 - pricefc_pinball_mean{env="$env", zone=~"$zone", window="$window"} / pricefc_naive_pinball_mean{env="$env", zone=~"$zone", window="$window"}', instant=True),
            target('pricefc_coverage_ratio{env="$env", zone=~"$zone", window="$window", band="90"}', instant=True),
            target('pricefc_scored_days{env="$env", zone=~"$zone", window="$window"}', instant=True),
        ], g, 16, 7, transformations=[
            {"id": "merge"},
            {"id": "organize", "options": {"excludeByName": {"Time": True, "__name__": True, "env": True, "host": True, "instance": True, "job": True, "window": True, "band": True},
                                           "renameByName": {"Value #A": "pinball", "Value #B": "skill vs naive", "Value #C": "90% coverage", "Value #D": "days"}}},
            {"id": "sortBy", "options": {"sort": [{"field": "zone"}]}},
        ], overrides=[
            {"matcher": {"id": "byName", "options": "skill vs naive"}, "properties": [{"id": "unit", "value": "percentunit"}]},
            {"matcher": {"id": "byName", "options": "90% coverage"}, "properties": [{"id": "unit", "value": "percentunit"},
                                                                                     {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                                                                     {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"value": None, "color": "red"}, {"value": 0.8, "color": "orange"}, {"value": 0.85, "color": "green"}, {"value": 0.95, "color": "orange"}]}}]},
            {"matcher": {"id": "byName", "options": "pinball"}, "properties": [{"id": "decimals", "value": 2}]},
        ]))
    ps.append(row("Over time", g))
    ps.append(panel(
        "timeseries", "Rolling pinball (EUR/MWh) vs backtest", [
            target('pricefc_pinball_mean{env="$env", zone=~"$zone", window="$window"}', "{{zone}} {{role}}"),
            target('pricefc_backtest_pinball{env="$env", zone=~"$zone"}', "{{zone}} backtest"),
        ], g, 12, 9, overrides=[
            {"matcher": {"id": "byRegexp", "options": ".*backtest"}, "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [6, 4]}}]},
        ], options={"tooltip": {"mode": "multi"}, "legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull"]}}))
    ps.append(panel(
        "timeseries", "90% band coverage", [
            target('pricefc_coverage_ratio{env="$env", zone=~"$zone", window="$window", band="90"}', "{{zone}} {{role}}"),
        ], g, 12, 9, unit="percentunit", desc="Share of hours whose actual price fell inside q05–q95. Target 0.90.",
        defaults={"min": 0.5, "max": 1, "custom": {"thresholdsStyle": {"mode": "line+area"}},
                  **thresholds((None, "transparent"), (0.85, "green"), (0.95, "transparent"))},
        options={"tooltip": {"mode": "multi"}}))
    ps.append(panel(
        "timeseries", "Skill vs naive (7-day-ago price)", [
            target('1 - pricefc_pinball_mean{env="$env", zone=~"$zone", window="$window"} / pricefc_naive_pinball_mean{env="$env", zone=~"$zone", window="$window"}', "{{zone}} {{role}}"),
        ], g, 24, 7, unit="percentunit", options={"tooltip": {"mode": "multi"}}))
    return dashboard("pricefc-quality", "pricefc · Forecast quality", ps, [env_var(), zone_var, window_var], "now-30d")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for build in (operations, quality):
        d = build()
        (OUT / f"{d['uid']}.json").write_text(json.dumps(d, indent=1, ensure_ascii=False) + "\n")
        print(f"wrote {d['uid']}.json ({len(d['panels'])} panels)")


if __name__ == "__main__":
    main()
