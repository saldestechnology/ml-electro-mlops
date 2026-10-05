#!/usr/bin/env bash
# One-time (idempotent) root setup of one environment on the VPS.
#   bootstrap.sh <env> <user> <mlflow_port> <prefect_port> <cron_morning> <cron_afternoon> \
#                <ci_pubkey_file> <admin_pubkey_file>
# The CI key may only run ~/bin/deploy (forced command, no shell, no forwarding).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
ENV=$1 USER_NAME=$2 MLFLOW_PORT=$3 PREFECT_PORT=$4 CRON_MORNING=$5 CRON_AFTERNOON=$6
CI_KEY=$(cat "$7") ADMIN_KEY=$(cat "$8")
HERE=$(cd "$(dirname "$0")" && pwd)

id "$USER_NAME" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "$USER_NAME"
passwd -l "$USER_NAME" >/dev/null
loginctl enable-linger "$USER_NAME"
H=$(getent passwd "$USER_NAME" | cut -d: -f6)

as_user() { runuser -u "$USER_NAME" -- "$@"; }
as_user mkdir -p "$H/bin" "$H/.config/pricefc" "$H/.config/containers/systemd" \
  "$H/pricefc/data" "$H/pricefc/mlflow" "$H/pricefc/prefect" "$H/.ssh"
chmod 700 "$H/.ssh" "$H/.config/pricefc"

install -m 755 -o "$USER_NAME" -g "$USER_NAME" "$HERE/deploy.sh" "$H/bin/deploy"

cat > "$H/.config/pricefc/deploy.env" <<EOF
ENV=$ENV
MLFLOW_PORT=$MLFLOW_PORT
PREFECT_PORT=$PREFECT_PORT
CRON_MORNING="$CRON_MORNING"
CRON_AFTERNOON="$CRON_AFTERNOON"
EOF
chown "$USER_NAME:$USER_NAME" "$H/.config/pricefc/deploy.env"
# Secrets (notification tokens etc.) live here, mode 600, never in git or the image.
[[ -e "$H/.config/pricefc/secrets.env" ]] || install -m 600 -o "$USER_NAME" -g "$USER_NAME" /dev/null "$H/.config/pricefc/secrets.env"

AK="$H/.ssh/authorized_keys"
{
  echo "command=\"$H/bin/deploy\",restrict $CI_KEY"
  echo "$ADMIN_KEY"
} > "$AK"
chown "$USER_NAME:$USER_NAME" "$AK"
chmod 600 "$AK"
echo "bootstrapped $ENV for $USER_NAME (mlflow 127.0.0.1:$MLFLOW_PORT, prefect 127.0.0.1:$PREFECT_PORT)"
