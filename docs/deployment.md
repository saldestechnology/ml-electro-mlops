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
  | forecast schedule (Stockholm) | 09:05 | 09:05 |
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

## Web dashboard

Install the optional `web` extra and run `pricefc web` locally; the API listens on
`127.0.0.1:8000`. To open a private pod's dashboard from your laptop, forward its loopback port
over SSH, for example `ssh -N -L 8000:127.0.0.1:8000 pricefc-vps`, then visit
`http://127.0.0.1:8000`. Keep the service bound to loopback on the pod; the tunnel provides
private access without exposing the dashboard publicly.

Status, logs, rollback (as the environment user):

```bash
ssh pricefc-vps '~/bin/deploy status'
ssh pricefc-vps 'journalctl --user -u pricefc-worker -n 100 --no-pager'
ssh pricefc-vps '~/bin/deploy rollback'
```

## Morning forecast (`forecast-daily`)

The Prefect flow `forecast-daily` (deployment `<env>-forecast`, cron `PRICEFC_CRON_FORECAST`,
default `5 9 * * *` Europe/Stockholm, i.e. just after the 09:00 origin so live inputs are
exactly what the origin allows) does, for origin D = today:

1. ingest prices and weather (`previous_runs`, `historical_forecast`, `single_runs`); a failed
   pull is reported but does not stop the run, stale inputs are caught by the live-row checks;
2. per zone, rebuild the `stitched` and `true_lead` datasets;
3. serve every configured champion for every zone before starting any configured challenger.
   Each role builds live rows of origin D (`pricefc.datasets.live`) and calls
   `pricefc.serving.live.forecast_origin`:
   - resolve its registry alias in `se-price-<zone>-hourly`; if the local state is missing or
     was pulled from another version, back it up and pull that version;
   - back up the state, advance it through every missed origin (from the `true_lead` rows) and
     D (from the live rows), write one forecast file per origin served, then save the state;
   - log a run in the MLflow experiment `forecast-live` with `role=champion` or
     `role=challenger`, zone, origin, model version, dataset versions, refit and the day's
     forecast artifact.

The pairs are configured in `configs/base.yaml` under `serving.models`: the champion is
`ensemble_hourly_exp` / alias `champion`, and the current shadow is
`ensemble_hourly_exp_tfm3` / alias `challenger`. If a zone has no challenger alias, it is
skipped with an info log. A challenger failure is logged and sent to Telegram, but does not
fail or replace any champion result. Champion or ingest failures still fail the flow at the end.
Other failed or crashed runs (this flow and `ingest-daily`) send a Telegram alert to "FC Mon"
with the flow, run and a one-line error (no tracebacks, no secrets).

Where things live (data root `~<user>/pricefc/data/` on the host, `/data` in the worker):

| path | content |
|---|---|
| `state/<zone>/ensemble_hourly_exp/` | served state (`state.pkl`, readable `state.json`) |
| `state/<zone>/ensemble_hourly_exp.backups/<origin>/` | state as it was after `<origin>` (newest 7 kept) |
| `forecasts/<zone>/<origin_date>.parquet` | D+1 quantile forecasts, with `model_version` and `forecast_made_at` (UTC) |
| `state/<zone>/ensemble_hourly_exp_tfm3/` | challenger state, independent of the champion state |
| `forecasts/<zone>/challenger/<origin_date>.parquet` | challenger D+1 quantiles; champion path remains unchanged |
| `live/<zone>/<origin_date>/` | live feature rows and their manifest |

**Bootstrap the challenger.** Build its state over the full evaluation year on the Apple-silicon
laptop (`backend: auto` selects MLX there). This is a long run: LightGBM refits as it does for
the champion and TimesFM runs at every origin. Keep the state outside `data/state` until it has
been reviewed; this command does not register it:

```bash
PYTHONPATH=$PWD/src /Users/johan/Documents/ml_code/electricity/.venv/bin/python -m pricefc state advance \
  --zone SE3 -m ensemble_hourly_exp_tfm3 \
  --first-origin 2025-10-03 \
  --dir /private/tmp/pricefc-se3-tfm3/state \
  --forecasts-out /private/tmp/pricefc-se3-tfm3/forecasts.parquet
```

The output parquet includes predictions and actual `y` values for pinball scoring. Compare it
with the champion backtest over exactly the same origins before registering. The saved TimesFM
3.0 backend stays `auto`: a laptop uses MLX when installed, and Linux resolves it to torch.
Compare both backends before deployment. If local torch is unavailable, run a one-origin preview
from the scratch state on the VPS against the MLX output before moving any alias.

**SE3 bootstrap result (2026-10-07).** The full state build completed 368 origins from
2025-10-03 through 2026-10-05 in 17m 59.7s, using dataset versions
`20261005-ef8ab3` (stitched) and `20261005-6744bc` (true lead). Mean pinball was 4.8414
EUR/MWh for the challenger and 5.2615 for the champion backtest over those same origins. A
torch-on-Mac preview from the saved state for origin 2026-10-05 differed from its MLX quantiles
by at most 0.000214 EUR/MWh. This is a one-origin parity check; repeat it on the VPS before
promotion. The scratch build was written to `/private/tmp/pricefc-se3-tfm3` and was not
registered.

After review, register the built state in the intended environment's MLflow registry:

```bash
PRICEFC_ENV_CONFIG=configs/envs/staging.yaml pricefc state register \
  --zone SE3 -m ensemble_hourly_exp_tfm3 \
  --dir /path/to/reviewed/state --alias challenger
```

This uses the configured registry and `licence_policy`; confirm the selected environment before
running it. To promote, point the `champion` alias at the reviewed challenger version and update
the `champion` entry in `serving.models` to `ensemble_hourly_exp_tfm3` in the deployment config.
Keep the old champion version under `challenger` for comparison or rollback. Reverse the alias
and config changes to roll back. Alias and configured spec must match because state directories
are keyed by model spec.

**Re-run a day.** Re-running is safe: if the state already served D, the forecast written then
is returned and nothing changes. Retry the whole flow from the Prefect UI, or one zone without
Prefect inside the worker container:

```bash
pricefc forecast -z SE3                    # today
pricefc forecast -z SE3 --day 2026-10-06   # a given origin (must be after the state's last one)
```

A missed day needs no action: the next run catches it up (from the `true_lead` dataset) before
forecasting D, and writes its forecast file too.

**Roll back the state.** To recompute a day already served (e.g. after bad live inputs), restore
the backup taken before it, then re-run. Forecasts for days after the restored origin are
recomputed and overwritten; the state keeps its `source_version`, so it is not pulled again.

```bash
cd ~/pricefc/data/state/SE3
ls ensemble_hourly_exp.backups/            # one directory per origin, newest last
mv ensemble_hourly_exp ensemble_hourly_exp.bad
cp -a ensemble_hourly_exp.backups/2026-10-05 ensemble_hourly_exp
pricefc forecast -z SE3 --day 2026-10-06
```

Moving the `champion` alias to another registered version is picked up by the next run (pulled,
then caught up from that version's last origin); to go back to the previous champion, move the
alias back.

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

`deploy.env` also holds `WEB_PORT` (the dashboard, published on the VPS loopback like MLflow
and Prefect; staging 8100) and optionally `CRON_FORECAST` (default `5 9 * * *`; production
runs after staging, e.g. `25 9 * * *`, so the two forecasts never share memory). Dashboard:
`ssh -N -L 8100:127.0.0.1:8100 pricefc-vps` then http://localhost:8100.

## One-time setup (already done, 2026-10-05)

As root on the VPS: `deploy/vps/bootstrap.sh <env> <user> <mlflow_port> <prefect_port>
<cron_morning> <cron_afternoon> <ci_pubkey_file> <admin_pubkey_file> <web_port>` creates the user (locked
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
