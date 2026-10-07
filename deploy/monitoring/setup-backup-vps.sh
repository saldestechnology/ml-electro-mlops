#!/usr/bin/env bash
# Idempotent setup of Prometheus, Alertmanager, node_exporter and Grafana on the backup VPS,
# as the unprivileged user pricefc-backup (systemd user services; everything on 127.0.0.1).
# Run from a copy of deploy/monitoring/. Re-run after changing any config here.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
MAIN_VPS=46.225.70.206
CONF=$HOME/.config/pricefc-monitoring UNITS=$HOME/.config/systemd/user

"$HERE/install.sh"
install -d -m 700 "$CONF" "$HOME/monitoring" "$HOME/.local/state/pricefc-metrics"
install -d -m 755 "$UNITS" "$CONF/grafana"
install -m 644 "$HERE/prometheus.yml" "$HERE/rules.yml" "$HERE/alertmanager.yml" "$HERE/grafana.ini" "$CONF/"
rm -rf "$CONF/grafana/provisioning" "$CONF/grafana/dashboards"
cp -r "$HERE/grafana/provisioning" "$HERE/grafana/dashboards" "$CONF/grafana/"
mkdir -p "$CONF/grafana/provisioning/plugins" "$CONF/grafana/provisioning/alerting"  # quiet startup errors
"$HOME/.local/opt/prometheus/promtool" check config "$CONF/prometheus.yml" >/dev/null
"$HOME/.local/opt/alertmanager/amtool" check-config "$CONF/alertmanager.yml" >/dev/null

[[ -f $CONF/grafana_admin_password ]] ||
  (umask 077 && head -c 24 /dev/urandom | base64 | tr -d '/+=' >"$CONF/grafana_admin_password")

# Tunnel key: on the main VPS it may only forward 127.0.0.1:8100 and :9100 (see docs).
[[ -f $HOME/.ssh/pricefc_tunnel ]] ||
  ssh-keygen -q -t ed25519 -N "" -C "pricefc_tunnel@$(hostname)" -f "$HOME/.ssh/pricefc_tunnel"
cfg=$HOME/.ssh/config
sed -i '/^# >>> pricefc-monitoring/,/^# <<< pricefc-monitoring/d' "$cfg"
cat >>"$cfg" <<EOT
# >>> pricefc-monitoring (managed by deploy/monitoring/setup-backup-vps.sh)
Host pricefc-tunnel-staging
    HostName $MAIN_VPS
    User pricefc-staging
    IdentityFile ~/.ssh/pricefc_tunnel
    IdentitiesOnly yes
    BatchMode yes
# <<< pricefc-monitoring
EOT

for unit in prometheus alertmanager node-exporter grafana pricefc-tunnel; do
  install -m 644 "$HERE/systemd/$unit.service" "$UNITS/"
done
systemctl --user daemon-reload
systemctl --user enable prometheus alertmanager node-exporter grafana
# Restart, not reload: a SIGHUP right after a fresh start can arrive before the handler exists.
systemctl --user restart prometheus alertmanager node-exporter grafana
# Refresh backup metrics now, so BackupMetricsMissing does not fire before the next backup.
"$HOME/.local/lib/pricefc-backup/run.sh" metrics || true
echo "tunnel key for authorized_keys of pricefc-staging on the main VPS:"
echo "  restrict,port-forwarding,permitopen=\"127.0.0.1:8100\",permitopen=\"127.0.0.1:9100\",command=\"/usr/sbin/nologin\",from=\"<this VPS's IPv4>\" $(cat "$HOME/.ssh/pricefc_tunnel.pub")"
echo "then: systemctl --user enable --now pricefc-tunnel"
