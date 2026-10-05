"""Code and environment lineage helpers (git state, file hashes)."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


def git_info(repo: Path | None = None) -> tuple[str, bool]:
    """Return (sha, dirty). In a container image built from a commit, PRICEFC_GIT_SHA carries
    the SHA (clean by construction). Falls back to ('unknown', True) otherwise."""
    baked = os.environ.get("PRICEFC_GIT_SHA", "")
    if baked and baked != "unknown":
        return baked, False
    cwd = repo or Path.cwd()
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown", True
    return sha, bool(status.strip())


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12] if path.exists() else "missing"
