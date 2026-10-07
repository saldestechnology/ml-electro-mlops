#!/usr/bin/env python3
"""Write the pod's container states as Prometheus text for node_exporter's textfile collector.

Runs as an environment user on the main VPS every minute (pricefc-container-metrics.timer);
reads only `podman ps` / `podman stats` for that user's rootless containers.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ENV = os.environ.get("PRICEFC_ENV", "staging")
OUT = Path.home() / ".local/state/pricefc-metrics/containers.prom"


UNITS = {
    "B": 1,
    "kB": 1e3,
    "KB": 1e3,
    "MB": 1e6,
    "GB": 1e9,
    "TB": 1e12,
    "KiB": 2**10,
    "MiB": 2**20,
    "GiB": 2**30,
}


def parse_bytes(text: str) -> int | None:
    """'258kB / 8.122GB' -> 258000 (podman prints decimal units)."""
    used = text.split("/")[0].strip()
    for unit in sorted(UNITS, key=len, reverse=True):
        if used.endswith(unit):
            try:
                return round(float(used[: -len(unit)]) * UNITS[unit])
            except ValueError:
                return None
    return None


def podman(*args: str) -> list[dict]:
    out = subprocess.run(
        ["podman", *args, "--format", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    return json.loads(out or "[]")


def main() -> int:
    lines = [
        "# HELP pricefc_container_running 1 if the container is running.",
        "# TYPE pricefc_container_running gauge",
    ]
    health, memory, restarts = [], [], []
    try:
        containers = [c for c in podman("ps", "-a") if c["Names"][0].startswith("pricefc-")]
        stats = {s.get("name") or s.get("Name"): s for s in podman("stats", "--no-stream", "-a")}
        ok = 1
    except Exception as exc:  # podman unavailable: say so instead of going silent
        print(f"podman failed: {exc}", file=sys.stderr)
        containers, stats, ok = [], {}, 0
    for c in containers:
        name = c["Names"][0]
        lab = f'env="{ENV}",name="{name}"'
        lines.append(f"pricefc_container_running{{{lab}}} {int(c.get('State') == 'running')}")
        status = c.get("Status", "")
        if "(healthy)" in status or "(unhealthy)" in status:
            health.append(f"pricefc_container_healthy{{{lab}}} {int('(healthy)' in status)}")
        restarts.append(f"pricefc_container_restarts{{{lab}}} {int(c.get('Restarts', 0) or 0)}")
        s = stats.get(name)
        used = parse_bytes(str(s.get("mem_usage", ""))) if s else None
        if used is not None:
            memory.append(f"pricefc_container_memory_bytes{{{lab}}} {used}")
    lines += [
        "# HELP pricefc_container_healthy 1 healthy, 0 unhealthy (containers with a healthcheck).",
        "# TYPE pricefc_container_healthy gauge",
        *health,
        "# HELP pricefc_container_restarts Restart count.",
        "# TYPE pricefc_container_restarts gauge",
        *restarts,
        "# HELP pricefc_container_memory_bytes Current memory use.",
        "# TYPE pricefc_container_memory_bytes gauge",
        *memory,
        "# HELP pricefc_container_metrics_ok 1 if podman could be queried.",
        "# TYPE pricefc_container_metrics_ok gauge",
        f'pricefc_container_metrics_ok{{env="{ENV}"}} {ok}',
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: node_exporter must never read a half-written file.
    with tempfile.NamedTemporaryFile("w", dir=OUT.parent, delete=False, suffix=".tmp") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(f.name, OUT)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
