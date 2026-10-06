#!/usr/bin/env bash
# Configure Vault for pricefc (idempotent). Run by the owner, once, after init + unseal:
#   ssh -t pricefc-vault bin/vault-setup
# Asks for the root token (not echoed, not stored). Creates:
#   - file audit log (/vault/logs/audit.log)
#   - KV v2 at secret/, with secrets under secret/pricefc/<env>/...
#   - per-environment read-only policy + AppRole (pricefc-production, pricefc-staging)
#   - policy `pricefc-operator` and a periodic token for routine automation (store secrets,
#     issue AppRole secret IDs), written to ~/.vault-operator-token (mode 600). It cannot
#     unseal, change policies or auth methods, or read other paths.
# Finally offers to revoke the root token (recommended; generate a new one with unseal keys
# via `vault operator generate-root` when needed).
set -euo pipefail
V=~/bin/vault
read -rsp "Vault root token: " VAULT_TOKEN; echo
export VAULT_TOKEN
$V token lookup >/dev/null

$V audit list 2>/dev/null | grep -q '^file/' || $V audit enable file file_path=/vault/logs/audit.log
$V secrets list | grep -q '^secret/' || $V secrets enable -path=secret -version=2 kv
$V auth list | grep -q '^approle/' || $V auth enable approle

for env in production staging; do
  $V policy write "pricefc-$env" - <<POL
path "secret/data/pricefc/$env/*" { capabilities = ["read"] }
POL
  $V write "auth/approle/role/pricefc-$env" \
    token_policies="pricefc-$env" token_ttl=1h token_max_ttl=24h \
    secret_id_ttl=0 secret_id_num_uses=0 >/dev/null
done

$V policy write pricefc-operator - <<'POL'
path "secret/data/pricefc/*"      { capabilities = ["create", "read", "update", "delete"] }
path "secret/metadata/pricefc/*"  { capabilities = ["read", "list", "delete"] }
path "auth/approle/role/pricefc-production/role-id"   { capabilities = ["read"] }
path "auth/approle/role/pricefc-staging/role-id"      { capabilities = ["read"] }
path "auth/approle/role/pricefc-production/secret-id" { capabilities = ["update"] }
path "auth/approle/role/pricefc-staging/secret-id"    { capabilities = ["update"] }
path "auth/approle/role/pricefc-production/secret-id-accessor/*" { capabilities = ["update"] }
path "auth/approle/role/pricefc-staging/secret-id-accessor/*"    { capabilities = ["update"] }
path "sys/policies/acl/pricefc-*" { capabilities = ["read"] }
POL

if [[ ! -s ~/.vault-operator-token ]]; then
  umask 077
  $V token create -orphan -policy=pricefc-operator -period=768h \
    -display-name=pricefc-operator -field=token > ~/.vault-operator-token
fi
chmod 600 ~/.vault-operator-token
echo "configured: audit, secret/ (kv v2), approle, policies, operator token"

read -rp "Revoke the root token now? [y/N] " ans
if [[ $ans == [yY] ]]; then
  $V token revoke -self && echo "root token revoked"
fi
