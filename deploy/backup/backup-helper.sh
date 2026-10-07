#!/usr/bin/env bash
# VPS side of the backups, installed as ~/bin/backup-helper for each environment's user.
#
# The backup VPS's key is restricted to this command (authorized_keys:
# command="~/bin/backup-helper",restrict,from="<backup VPS>"), so it can do exactly three things:
#   snapshot   SQLite online backup of MLflow and Prefect inside their containers
#   cleanup    remove those backup copies again
#   rsync      read-only rsync of ~/pricefc/ (rrsync -ro; nothing else in $HOME is reachable)
# An admin key may call it directly: `ssh <host> bin/backup-helper snapshot`.
set -euo pipefail

read -r -a words <<<"${SSH_ORIGINAL_COMMAND:-$*}"
# "bin/backup-helper snapshot" from the client: drop the program name.
[[ "${words[0]:-}" == *backup-helper ]] && words=("${words[@]:1}")

sqlite_backup() {
  # <container> <db> <copy>; the paths are fixed here, never taken from the client.
  podman exec "$1" python -c "import sqlite3; s = sqlite3.connect('$2'); d = sqlite3.connect('$3'); s.backup(d); d.close(); s.close()"
}

case "${words[0]:-}" in
  snapshot)
    sqlite_backup pricefc-mlflow /data/mlflow/mlflow.db /data/mlflow/backup.db
    sqlite_backup pricefc-prefect /data/prefect/prefect.db /data/prefect/backup.db
    ;;
  cleanup)
    rm -f "$HOME/pricefc/mlflow/backup.db" "$HOME/pricefc/prefect/backup.db"
    ;;
  rsync)
    # rrsync re-reads SSH_ORIGINAL_COMMAND, allows only a sending rsync server, and confines
    # every path to ~/pricefc.
    [[ "${words[1]:-}" == --server && " ${words[*]} " == *" --sender "* ]] || {
      echo "backup-helper: only read-only rsync is allowed" >&2
      exit 1
    }
    exec /usr/bin/rrsync -ro "$HOME/pricefc"
    ;;
  *)
    echo "backup-helper: unknown command '${words[0]:-}' (snapshot | cleanup | rsync)" >&2
    exit 1
    ;;
esac
