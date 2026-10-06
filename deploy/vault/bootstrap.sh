#!/usr/bin/env bash
# One-time (idempotent) root setup of the Vault server user. Run as root from the repo checkout:
#   deploy/vault/bootstrap.sh <admin_pubkey_file>
# Initialisation and unsealing are done by the owner afterwards (see docs/deployment.md).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
ADMIN_KEY=$(cat "$1")
HERE=$(cd "$(dirname "$0")" && pwd)
U=vault

id "$U" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "$U"
passwd -l "$U" >/dev/null
loginctl enable-linger "$U"
H=$(getent passwd "$U" | cut -d: -f6)
as_user() { runuser -u "$U" -- "$@"; }
as_user mkdir -p "$H/bin" "$H/vault/config" "$H/vault/file" "$H/vault/logs" \
  "$H/.config/containers/systemd" "$H/.ssh"
chmod 700 "$H/vault" "$H/.ssh"
install -m 644 -o "$U" -g "$U" "$HERE/vault.hcl" "$H/vault/config/vault.hcl"
install -m 644 -o "$U" -g "$U" "$HERE/vault.container" "$H/.config/containers/systemd/vault.container"
install -m 755 -o "$U" -g "$U" "$HERE/vault-cli.sh" "$H/bin/vault"
install -m 755 -o "$U" -g "$U" "$HERE/setup.sh" "$H/bin/vault-setup"
echo "$ADMIN_KEY" > "$H/.ssh/authorized_keys"
chown "$U:$U" "$H/.ssh/authorized_keys"; chmod 600 "$H/.ssh/authorized_keys"

uid=$(id -u "$U")
export XDG_RUNTIME_DIR=/run/user/$uid
until [[ -S $XDG_RUNTIME_DIR/bus ]]; do sleep 1; done
as_user env XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" systemctl --user daemon-reload
as_user env XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" systemctl --user restart vault.service
echo "vault running on 127.0.0.1:8200 (sealed until initialised and unsealed by the owner)"
