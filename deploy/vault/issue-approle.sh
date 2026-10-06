#!/usr/bin/env bash
# Issue (or rotate) the Vault AppRole credentials of one environment and install them for its
# Vault Agent. Run as root on the VPS; uses the vault user's operator token, so the values never
# leave the machine:
#   ssh pricefc-vps-root 'bash -s' < deploy/vault/issue-approle.sh <env> <user>
# Rotation: run again, then `systemctl --user restart pricefc-vault-agent` as <user>. Old
# secret-ids stay valid until destroyed (vault write auth/approle/role/<role>/secret-id-accessor/destroy).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
ENV=$1 USER_NAME=$2
[[ $ENV =~ ^(production|staging)$ ]] || { echo "env must be production or staging" >&2; exit 1; }
VH=$(getent passwd vault | cut -d: -f6)
H=$(getent passwd "$USER_NAME" | cut -d: -f6)
DIR="$H/.config/pricefc/vault"

VAULT_TOKEN=$(cat "$VH/.vault-operator-token")
# runuser does not start a login session: point podman at the vault user's systemd/D-Bus.
XDG_RUNTIME_DIR=/run/user/$(id -u vault)
DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
export VAULT_TOKEN XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS
# stdin from /dev/null: under `bash -s`, podman exec -i would otherwise eat the rest of the script.
v() { (cd "$VH" && runuser -u vault -- "$VH/bin/vault" "$@" </dev/null); }

install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$DIR"
umask 077
v read -field=role_id "auth/approle/role/pricefc-$ENV/role-id" > "$DIR/role-id"
v write -f -field=secret_id "auth/approle/role/pricefc-$ENV/secret-id" > "$DIR/secret-id"
chown "$USER_NAME:$USER_NAME" "$DIR/role-id" "$DIR/secret-id"
echo "issued AppRole pricefc-$ENV credentials to $DIR"
