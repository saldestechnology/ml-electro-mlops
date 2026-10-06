#!/usr/bin/env bash
# Pull a backup of one VPS environment onto this machine (the VPS gets no access here).
#
#   tools/backup_vps.sh [env ...]          default: staging (add production once it runs)
#   PRICEFC_BACKUP_DIR=/Volumes/External/Backups/pricefc   where snapshots go (outside the repo)
#
# Each run makes a dated snapshot <dir>/<env>/<UTC timestamp>/ with rsync --link-dest against
# the previous one, so unchanged files are hard links and cost no space. SQLite databases
# (MLflow, Prefect) are copied with SQLite's online backup API inside their own containers
# first: copying a live database file can capture a torn write. Kept: the newest 14 snapshots
# plus the newest one of each of the last 8 ISO weeks.
#
# Not backed up: hf-cache (re-downloadable model weights) and Vault (its raft snapshots need
# the owner's operator token; see docs/deployment.md).
set -euo pipefail

DEST="${PRICEFC_BACKUP_DIR:-/Volumes/External/Backups/pricefc}"
KEEP_DAILY=14
KEEP_WEEKLY=8

host_for() {
  case "$1" in
    staging) echo pricefc-vps-staging ;;
    production) echo pricefc-vps ;;
    *) echo "unknown env $1" >&2; return 1 ;;
  esac
}

# SQLite online backup inside a container: <container> <db path> <backup path>.
sqlite_backup() {
  ssh "$HOST" "podman exec $1 python -c \"import sqlite3; s = sqlite3.connect('$2'); d = sqlite3.connect('$3'); s.backup(d); d.close(); s.close()\""
}

prune() {
  # Plain bash 3.2 (macOS /bin/bash): no mapfile, no associative arrays.
  local dir=$1 keep=" " weeks=" " nweeks=0 n=0 snap week
  local snaps
  snaps=$(find "$dir" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort -r)
  for snap in $snaps; do
    n=$((n + 1))
    ((n <= KEEP_DAILY)) && keep="$keep$snap "
  done
  for snap in $snaps; do
    week=$(date -j -u -f '%Y%m%dT%H%M%SZ' "$(basename "$snap")" +%G-%V 2>/dev/null) || continue
    if [[ "$weeks" != *" $week "* ]] && ((nweeks < KEEP_WEEKLY)); then
      weeks="$weeks$week "
      nweeks=$((nweeks + 1))
      keep="$keep$snap "
    fi
  done
  for snap in $snaps; do
    [[ "$keep" == *" $snap "* ]] || rm -rf "$snap"
  done
}

# An unplugged external drive leaves /Volumes/<name> absent (or an empty stub on the system
# disk); writing there would fill the internal disk and look like a backup. Refuse instead.
if [[ "$DEST" == /Volumes/* ]]; then
  vol="/Volumes/$(cut -d/ -f3 <<<"$DEST")"
  if [[ ! -d "$vol" ]] || [[ "$(stat -f %d "$vol")" == "$(stat -f %d /)" ]]; then
    echo "[backup] $vol is not mounted; plug in the drive (nothing was backed up)" >&2
    exit 2
  fi
fi

for ENV in "${@:-staging}"; do
  HOST=$(host_for "$ENV")
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  root="$DEST/$ENV"
  mkdir -p "$root"
  echo "[backup:$ENV] database snapshots"
  sqlite_backup pricefc-mlflow /data/mlflow/mlflow.db /data/mlflow/backup.db
  sqlite_backup pricefc-prefect /data/prefect/prefect.db /data/prefect/backup.db

  link=--link-dest=/nonexistent
  [[ -d "$root/latest" ]] && link="--link-dest=$root/latest/"
  tmp="$root/.partial-$stamp"
  echo "[backup:$ENV] pulling into $root/$stamp"
  rsync -a --delete "$link" \
    --exclude 'data/hf-cache/' --exclude '.*.tmp' --exclude '*.tmp' \
    --exclude 'mlflow/mlflow.db*' --exclude 'prefect/prefect.db*' \
    "$HOST:pricefc/" "$tmp/"
  mv "$tmp/mlflow/backup.db" "$tmp/mlflow/mlflow.db"
  mv "$tmp/prefect/backup.db" "$tmp/prefect/prefect.db"
  ssh "$HOST" 'rm -f pricefc/mlflow/backup.db pricefc/prefect/backup.db'

  # Restorable: both databases pass SQLite's integrity check.
  for db in "$tmp/mlflow/mlflow.db" "$tmp/prefect/prefect.db"; do
    [[ "$(sqlite3 "$db" 'pragma integrity_check')" == ok ]] || {
      echo "[backup:$ENV] integrity check failed: $db (kept in $tmp)" >&2
      exit 1
    }
  done
  mv "$tmp" "$root/$stamp"
  ln -sfn "$stamp" "$root/latest"
  prune "$root"
  echo "[backup:$ENV] ok: $(du -sh "$root/$stamp" | cut -f1) in $root/$stamp," \
    "$(find "$root" -mindepth 1 -maxdepth 1 -type d -name '20*' | wc -l | tr -d ' ') snapshots kept"
done
