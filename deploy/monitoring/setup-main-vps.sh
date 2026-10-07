#!/usr/bin/env bash
# Idempotent setup of node_exporter (127.0.0.1:9100) and the container metrics timer on the
# main VPS, as the staging user (one node_exporter per host; host metrics are host-wide).
# Run from a copy of deploy/monitoring/.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
UNITS=$HOME/.config/systemd/user
"$HERE/install.sh" node_exporter
install -d -m 755 "$HOME/.local/lib/pricefc-monitoring" "$UNITS"
install -m 755 "$HERE/container-metrics.py" "$HOME/.local/lib/pricefc-monitoring/"
for unit in node-exporter.service pricefc-container-metrics.service pricefc-container-metrics.timer; do
  install -m 644 "$HERE/systemd/$unit" "$UNITS/"
done
systemctl --user daemon-reload
systemctl --user enable --now node-exporter.service pricefc-container-metrics.timer
