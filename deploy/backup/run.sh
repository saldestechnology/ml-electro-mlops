#!/usr/bin/env bash
# Backup VPS: nightly pull from the main VPS, then an encrypted off-site copy on the Storage Box.
#
#   run.sh backup          pull every env in $PRICEFC_BACKUP_ENVS, then restic backup + forget
#   run.sh check           weekly: restic check (10% of the data) + restore test of MLflow
#   run.sh metrics         only rewrite the textfile metrics from the last-ok stamps
#
# Runs as the unprivileged user pricefc-backup (systemd user timers). Settings come from
# ~/.config/pricefc-backup/backup.env (setup.sh writes it). A failure sends a Telegram alert when
# ~/.config/pricefc-backup/telegram.env exists (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID; mode 600,
# never printed). Success writes $STATE/last-ok-<mode>, which the monitoring reads for
# "no backup in 36 h".
set -euo pipefail

CONF=${PRICEFC_BACKUP_CONF:-$HOME/.config/pricefc-backup}
# Exported: backup_vps.sh and restic read these from the environment.
set -a
# shellcheck source=/dev/null
. "$CONF/backup.env"
set +a
MODE=${1:-backup}
STATE=${PRICEFC_BACKUP_STATE:-$HOME/.local/state/pricefc-backup}
mkdir -p "$STATE"

alert() {
  local tg=$CONF/telegram.env
  [[ -r $tg ]] || return 0
  (
    # shellcheck source=/dev/null
    . "$tg"
    # The token goes in a curl config on stdin, so it never shows up in the process list.
    curl -sS -m 20 -o /dev/null -K - --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" \
      --data-urlencode "text=[backup $(hostname)] $1" \
      <<<"url = \"https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage\""
  ) || echo "telegram alert failed" >&2
}

fail() {
  echo "FAILED: $1" >&2
  alert "FAILED ($MODE): $1"
  exit 1
}

backup() {
  local env
  for env in $PRICEFC_BACKUP_ENVS; do
    "$PRICEFC_BACKUP_HOME/backup_vps.sh" "$env" || fail "pull of $env failed (see journalctl --user -u pricefc-backup)"
    # A stable path (hard-link copy of the newest snapshot) lets restic find its parent
    # snapshot and only read changed files.
    rm -rf "$PRICEFC_BACKUP_DIR/$env/.offsite"
    cp -al "$(readlink -f "$PRICEFC_BACKUP_DIR/$env/latest")" "$PRICEFC_BACKUP_DIR/$env/.offsite"
    restic backup --quiet --host pricefc-backup --tag "$env" "$PRICEFC_BACKUP_DIR/$env/.offsite" ||
      fail "restic backup of $env to the Storage Box failed"
  done
  restic forget --quiet --prune --group-by host,tags \
    --keep-daily 14 --keep-weekly 8 --keep-monthly 12 || fail "restic forget/prune failed"
}

check() {
  restic check --read-data-subset=10% || fail "restic check found a problem"
  local env tmp db
  for env in $PRICEFC_BACKUP_ENVS; do
    tmp=$(mktemp -d)
    restic restore latest --tag "$env" --target "$tmp" \
      --include "$PRICEFC_BACKUP_DIR/$env/.offsite/mlflow/mlflow.db" >/dev/null ||
      { rm -rf "$tmp"; fail "restore of $env from the Storage Box failed"; }
    db="$tmp$PRICEFC_BACKUP_DIR/$env/.offsite/mlflow/mlflow.db"
    # Restorable means: intact, and the served models are in it.
    [[ "$(sqlite3 "$db" 'pragma integrity_check')" == ok ]] ||
      { rm -rf "$tmp"; fail "restored $env mlflow.db fails integrity_check"; }
    n=$(sqlite3 "$db" "select count(*) from registered_model_aliases where alias = 'champion'")
    rm -rf "$tmp"
    ((n > 0)) || fail "restored $env mlflow.db has no champion alias"
    echo "[check:$env] restore ok ($n champion aliases)"
  done
}

# Last successes as Prometheus text for node_exporter's textfile collector (monitoring).
export_metrics() {
  local dir=$HOME/.local/state/pricefc-metrics m t
  mkdir -p "$dir"
  {
    echo "# HELP pricefc_backup_last_success_timestamp_seconds Last successful run per mode."
    echo "# TYPE pricefc_backup_last_success_timestamp_seconds gauge"
    for m in backup check; do
      [[ -f $STATE/last-ok-$m ]] || continue
      t=$(date -u -d "$(cat "$STATE/last-ok-$m")" +%s)
      echo "pricefc_backup_last_success_timestamp_seconds{mode=\"$m\"} $t"
    done
    echo "# HELP pricefc_backup_local_bytes Size of the local snapshot directory."
    echo "# TYPE pricefc_backup_local_bytes gauge"
    echo "pricefc_backup_local_bytes $(du -sb "$PRICEFC_BACKUP_DIR" | cut -f1)"
  } >"$dir/backup.prom.tmp"
  mv "$dir/backup.prom.tmp" "$dir/backup.prom"
}

case "$MODE" in
  backup) backup ;;
  check) check ;;
  metrics) export_metrics; exit 0 ;;
  *) fail "unknown mode $MODE" ;;
esac
date -u +%FT%TZ >"$STATE/last-ok-$MODE"
export_metrics
echo "[$MODE] ok"
