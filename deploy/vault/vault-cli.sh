#!/usr/bin/env bash
# `vault` CLI for the vault user: runs inside the server container. Installed as ~/bin/vault.
#   ssh -t pricefc-vault bin/vault status
set -euo pipefail
tty=()
[[ -t 0 && -t 1 ]] && tty=(-t)
exec podman exec -i "${tty[@]}" -e VAULT_ADDR=http://127.0.0.1:8200 \
  ${VAULT_TOKEN:+-e VAULT_TOKEN} vault vault "$@"
