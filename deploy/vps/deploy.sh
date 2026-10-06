#!/usr/bin/env bash
# Deploy entry point for one environment on the VPS. Installed as ~/bin/deploy for the
# environment's user and bound to the CI deploy key with a forced command, so that key can only
# run:   deploy <ghcr image@sha256 digest> | status | rollback
#
# deploy: pull the image, keep the running one as :previous, install the Quadlet units shipped
#         inside the image, restart the pod and wait for health; roll back on failure.
set -euo pipefail

IMAGE_RE='^ghcr\.io/saldestechnology/ml-electro-mlops@sha256:[0-9a-f]{64}$'
CONF="$HOME/.config/pricefc/deploy.env"   # ENV, MLFLOW_PORT, PREFECT_PORT, CRON_MORNING, CRON_AFTERNOON
UNITS="$HOME/.config/containers/systemd"
CURRENT=localhost/pricefc:current
PREVIOUS=localhost/pricefc:previous
HEALTH_TIMEOUT=240   # seconds for MLflow, Prefect and the worker to come up

# shellcheck source=/dev/null
source "$CONF"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

log() { echo "[deploy:$ENV] $*"; }
die() { echo "[deploy:$ENV] ERROR: $*" >&2; exit 1; }

install_units() {
  # Units travel with the image, so code and its runtime config are always the same version.
  local tmp
  tmp=$(mktemp -d)
  podman run --rm --entrypoint sh "$CURRENT" -c 'tar -C /app/deploy/quadlet -cf - .' | tar -C "$tmp" -xf -
  mkdir -p "$UNITS"
  for f in "$tmp"/*; do
    sed -e "s|@ENV@|$ENV|g" -e "s|@MLFLOW_PORT@|$MLFLOW_PORT|g" \
        -e "s|@PREFECT_PORT@|$PREFECT_PORT|g" -e "s|@CRON_MORNING@|$CRON_MORNING|g" \
        -e "s|@CRON_AFTERNOON@|$CRON_AFTERNOON|g" "$f" > "$UNITS/$(basename "$f")"
  done
  rm -rf "$tmp"
  # Vault Agent config (role-id/secret-id next to it are issued once, not shipped).
  install -d -m 700 "$HOME/.config/pricefc/vault"
  podman run --rm --entrypoint cat "$CURRENT" /app/deploy/vault/agent.hcl > "$HOME/.config/pricefc/vault/agent.hcl"
  systemctl --user daemon-reload
}

restart() {
  systemctl --user restart pricefc-pod.service
  systemctl --user restart pricefc-vault-agent.service pricefc-mlflow.service pricefc-prefect.service \
    pricefc-worker.service
}

healthy() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  while ((SECONDS < deadline)); do
    if curl -fsS "http://127.0.0.1:$MLFLOW_PORT/health" >/dev/null 2>&1 \
      && curl -fsS "http://127.0.0.1:$PREFECT_PORT/api/health" >/dev/null 2>&1 \
      && systemctl --user is-active --quiet pricefc-worker.service; then
      return 0
    fi
    sleep 5
  done
  return 1
}

status() {
  echo "env=$ENV"
  echo "current=$(podman image inspect "$CURRENT" --format '{{index .Labels "org.opencontainers.image.revision"}} {{.Digest}}' 2>/dev/null || echo none)"
  echo "previous=$(podman image inspect "$PREVIOUS" --format '{{index .Labels "org.opencontainers.image.revision"}} {{.Digest}}' 2>/dev/null || echo none)"
  for s in pricefc-vault-agent pricefc-mlflow pricefc-prefect pricefc-worker; do
    echo "$s=$(systemctl --user is-active "$s.service" 2>/dev/null || true)"
  done
}

rollback() {
  podman image exists "$PREVIOUS" || die "no previous image to roll back to"
  podman tag "$PREVIOUS" "$CURRENT"
  install_units
  restart
  healthy || die "rollback did not become healthy"
  log "rolled back"
}

cmd="${SSH_ORIGINAL_COMMAND:-$*}"
read -r action ref extra <<<"$cmd" || true
case "${action:-}" in
  deploy)
    [[ -z "${extra:-}" && "${ref:-}" =~ $IMAGE_RE ]] || die "usage: deploy ghcr.io/...@sha256:<digest>"
    log "pulling $ref"
    podman pull -q "$ref" >/dev/null
    if podman image exists "$CURRENT"; then podman tag "$CURRENT" "$PREVIOUS"; fi
    podman tag "$ref" "$CURRENT"
    install_units
    restart
    if healthy; then
      log "healthy: $(podman image inspect "$CURRENT" --format '{{index .Labels "org.opencontainers.image.revision"}}')"
      # Keep this script in step with the deployed version (the image already runs as us).
      podman run --rm --entrypoint cat "$CURRENT" /app/deploy/vps/deploy.sh > "$HOME/bin/deploy.new" \
        && chmod 755 "$HOME/bin/deploy.new" && mv "$HOME/bin/deploy.new" "$HOME/bin/deploy"
      podman image prune -f >/dev/null || true
    else
      log "unhealthy after deploy; rolling back"
      journalctl --user -u pricefc-worker.service -n 30 --no-pager || true
      rollback
      exit 1
    fi
    ;;
  status) status ;;
  rollback) rollback ;;
  *) die "allowed: deploy <image@digest> | status | rollback" ;;
esac
