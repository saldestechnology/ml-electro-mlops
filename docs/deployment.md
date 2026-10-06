# Deployment and operations

## Overview

```
push to main ──► CI ──► image (GHCR, tag sha-<commit>) ──► staging (automatic)
publish release vX.Y.Z ──► production: same image digest, after approval (GitHub Environment)
```

- **Image**: `ghcr.io/saldestechnology/ml-electro-mlops`, built once per commit from
  `docker/Containerfile` (uv.lock, CPU-only torch). Production never rebuilds; it deploys the
  digest that went through staging, and the workflow refuses a commit without a successful
  staging deployment.
- **VPS**: one unprivileged Linux user per environment, each with rootless podman and a pod
  `pricefc` (Quadlet units, shipped inside the image under `deploy/quadlet/`):

  | | production | staging |
  |---|---|---|
  | user | `pricefc` | `pricefc-staging` |
  | MLflow (loopback) | 127.0.0.1:5000 | 127.0.0.1:5100 |
  | Prefect UI (loopback) | 127.0.0.1:4200 | 127.0.0.1:4300 |
  | ingest schedule (Stockholm) | 08:15, 13:35 | 08:00, 13:20 |
  | data | `~pricefc/pricefc/` | `~pricefc-staging/pricefc/` |

- **Deploy**: GitHub Actions SSHes in with an environment-specific key that is bound to
  `~/bin/deploy` (forced command, no shell, no forwarding). `deploy` pulls the digest, keeps the
  running image as `:previous`, installs the units from the image, restarts the pod, waits for
  health (MLflow, Prefect, worker) and rolls back automatically if it does not become healthy.

## Everyday tasks

Open the UIs through a tunnel (nothing is exposed publicly):

```bash
ssh -N -L 5000:127.0.0.1:5000 -L 4200:127.0.0.1:4200 pricefc-vps          # production
ssh -N -L 5100:127.0.0.1:5100 -L 4300:127.0.0.1:4300 pricefc-vps-staging  # staging
```

Release to production: create a GitHub release with a `v*` tag on a commit that is already on
staging, then approve the `production` deployment in the Actions run.

Status, logs, rollback (as the environment user):

```bash
ssh pricefc-vps '~/bin/deploy status'
ssh pricefc-vps 'journalctl --user -u pricefc-worker -n 100 --no-pager'
ssh pricefc-vps '~/bin/deploy rollback'
```

## Secrets (HashiCorp Vault)

Vault runs on the VPS as user `vault` (`deploy/vault/`, loopback 127.0.0.1:8200). Secrets live
at `secret/pricefc/<env>/<name>`; a Vault Agent container in each pod renders them as files in
`/run/secrets` (tmpfs), read with `pricefc.secrets.get_secret`. Nothing secret goes in git, the
image or `deploy.env`.

| path | keys | used by |
|---|---|---|
| `secret/pricefc/<env>/telegram` | `bot_token`, `chat_id` | alerts (group "FC Mon" only) |

**After a Vault restart (or VPS reboot) the owner must unseal it**: run in your own terminal,
three times with three different keys,

```bash
ssh -t pricefc-vault bin/vault operator unseal
```

The agents pick it up within a minute. Until then forecasts keep running but alerts cannot be
sent. Check with `ssh pricefc-vault bin/vault status`.

Write or rotate a secret (on the VPS, as `vault`, with the operator token; values from stdin
as JSON so they never appear in argv or shell history):

```bash
ssh pricefc-vault 'VAULT_TOKEN=$(cat ~/.vault-operator-token) bin/vault kv put secret/pricefc/staging/telegram -' <<< '{"bot_token":"...","chat_id":"..."}'
```

AppRole credentials for an environment's agent (issue or rotate):
`ssh pricefc-vps-root 'bash -s' < deploy/vault/issue-approle.sh <env> <user>`.

## One-time setup (already done, 2026-10-05)

As root on the VPS: `deploy/vps/bootstrap.sh <env> <user> <mlflow_port> <prefect_port>
<cron_morning> <cron_afternoon> <ci_pubkey_file> <admin_pubkey_file>` creates the user (locked
password, linger, no sudo), directories, `deploy.env`, the Vault credentials directory and
`authorized_keys`. GitHub Environments `staging` (branch `main`) and `production` (tags `v*`,
required reviewer) hold `DEPLOY_SSH_KEY`, `VPS_KNOWN_HOSTS` and the `DEPLOY_TARGET` variable.

Vault (as root, once): `deploy/vault/bootstrap.sh <admin_pubkey_file>` creates user `vault` and
starts the server. The owner then initialises it (`ssh -t pricefc-vault bin/vault operator
init`, keys kept offline), unseals it and runs `ssh -t pricefc-vault bin/vault-setup` (audit
log, KV, AppRoles, policies, operator token, revokes the root token).

## Constraints

The VPS is shared with other workloads. Do not reboot, run a full upgrade, or change SSH or
firewall settings without the owner. Disk is limited (~15 GB free): images share their
dependency layer, and `deploy` prunes dangling images after a healthy deploy.
