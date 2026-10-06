"""Secrets for flows (notification tokens etc.).

On the VPS a Vault Agent sidecar renders each secret as one file in a tmpfs directory mounted
at ``/run/secrets`` (deploy/vault/agent.hcl). An environment variable of the upper-cased name
overrides the file, for local runs and tests.
"""

import os
from pathlib import Path


def secrets_dir() -> Path:
    return Path(os.environ.get("PRICEFC_SECRETS_DIR", "/run/secrets"))


def get_secret(name: str) -> str | None:
    """The secret ``name`` (e.g. ``telegram_bot_token``), or None if it is not available.

    None covers a sealed or unreachable Vault before the first render: callers that only
    notify should skip, not fail the flow.
    """
    if value := os.environ.get(name.upper()):
        return value
    try:
        value = (secrets_dir() / name).read_text().strip()
    except (FileNotFoundError, PermissionError):
        return None
    return value or None
