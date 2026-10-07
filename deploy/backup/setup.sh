#!/usr/bin/env bash
# Idempotent setup of the backups on the backup VPS, run as the unprivileged user
# pricefc-backup from a copy of deploy/backup/ that also contains tools/backup_vps.sh:
#   setup.sh <storagebox user> <storagebox host> [envs, default "staging"]
#
# Root does only the one-time part (docs/deployment.md "Backups"): restic, rsync, sqlite3 and
# curl installed, the user created with linger and in sshd's AllowUsers.
#
# Creates two keys: pricefc_pull (main VPS, restricted to ~/bin/backup-helper there) and
# pricefc_storagebox (Storage Box sub-account). Prints both public keys; installing them is a
# separate, explicit step. The restic repository password is generated once into
# ~/.config/pricefc-backup/restic.pass; the owner keeps a copy elsewhere, since without it the
# Storage Box copy cannot be read.
set -euo pipefail
SB_USER=$1 SB_HOST=$2 ENVS=${3:-staging}
HERE=$(cd "$(dirname "$0")" && pwd)
MAIN_VPS=46.225.70.206
APP=$HOME/.local/lib/pricefc-backup CONF=$HOME/.config/pricefc-backup DATA=$HOME/backups
UNITS=$HOME/.config/systemd/user

for tool in restic rsync sqlite3 curl; do
  command -v "$tool" >/dev/null || { echo "missing $tool (root installs it once)" >&2; exit 1; }
done

install -d -m 700 "$APP" "$CONF" "$DATA" "$HOME/.local/state/pricefc-backup" "$HOME/.ssh" "$UNITS"
install -m 755 "$HERE/run.sh" "$APP/run.sh"
install -m 755 "$HERE/backup_vps.sh" "$APP/backup_vps.sh"

for key in pricefc_pull pricefc_storagebox; do
  [[ -f $HOME/.ssh/$key ]] || ssh-keygen -q -t ed25519 -N "" -C "$key@$(hostname)" -f "$HOME/.ssh/$key"
done

# Host keys are pinned on first setup (compare with the fingerprints in docs/deployment.md).
kh=$HOME/.ssh/known_hosts
touch "$kh"
grep -q "^$MAIN_VPS " "$kh" || ssh-keyscan -t ed25519 "$MAIN_VPS" >>"$kh" 2>/dev/null
grep -q "^\[$SB_HOST\]:23 " "$kh" || ssh-keyscan -t ed25519 -p 23 "$SB_HOST" >>"$kh" 2>/dev/null

# Managed block in the user's ssh config: the aliases backup_vps.sh and restic use.
cfg=$HOME/.ssh/config
touch "$cfg"
sed -i '/^# >>> pricefc-backup/,/^# <<< pricefc-backup/d' "$cfg"
cat >>"$cfg" <<EOF
# >>> pricefc-backup (managed by deploy/backup/setup.sh)
Host pricefc-vps-staging
    HostName $MAIN_VPS
    User pricefc-staging
Host pricefc-vps
    HostName $MAIN_VPS
    User pricefc
Host pricefc-vps-staging pricefc-vps
    IdentityFile ~/.ssh/pricefc_pull
    IdentitiesOnly yes
    BatchMode yes
Host pricefc-storagebox
    HostName $SB_HOST
    Port 23
    User $SB_USER
    IdentityFile ~/.ssh/pricefc_storagebox
    IdentitiesOnly yes
    BatchMode yes
    ServerAliveInterval 30
# <<< pricefc-backup
EOF
chmod 600 "$cfg"

[[ -f $CONF/restic.pass ]] || (umask 077 && head -c 32 /dev/urandom | base64 >"$CONF/restic.pass")
cat >"$CONF/backup.env" <<EOF
PRICEFC_BACKUP_ENVS="$ENVS"
PRICEFC_BACKUP_HOME=$APP
PRICEFC_BACKUP_DIR=$DATA
# Behind the restricted key, rrsync roots the remote side at ~/pricefc.
PRICEFC_BACKUP_REMOTE_DIR=./
# The Storage Box keeps the long history; locally a week of fast restores is enough.
PRICEFC_BACKUP_KEEP_DAILY=7
PRICEFC_BACKUP_KEEP_WEEKLY=0
RESTIC_REPOSITORY=sftp:pricefc-storagebox:restic
RESTIC_PASSWORD_FILE=$CONF/restic.pass
EOF
chmod 600 "$CONF/backup.env"

for unit in pricefc-backup.service pricefc-backup.timer pricefc-backup-check.service pricefc-backup-check.timer; do
  install -m 644 "$HERE/$unit" "$UNITS/$unit"
done
systemctl --user daemon-reload
# Timers are enabled after the keys are installed and `restic init` ran (docs/deployment.md):
#   systemctl --user enable --now pricefc-backup.timer pricefc-backup-check.timer

echo "main VPS key (authorized_keys of each env user, restricted):"
echo "  command=\"/home/<user>/bin/backup-helper\",restrict,from=\"<this VPS's IPv4>\" $(cat "$HOME/.ssh/pricefc_pull.pub")"
echo "Storage Box key (sub-account $SB_USER):"
echo "  $(cat "$HOME/.ssh/pricefc_storagebox.pub")"
