"""Repo clone helpers and forge clone-error classification."""
from __future__ import annotations

import os
import subprocess

def clone_extra_repos(extras, repo_dir, runner=None):
    """Read-only sibling clones for multi-repo ONBOARD triage (item 2 full
    scope): each extra repo rides its OWN read token via the shared askpass
    script (per-clone env override). Shallow (--depth 1) — assessment only.
    A failed extra clone is deliberately NON-fatal: triage proceeds on what
    cloned, and the failures are returned for the transcript/log."""
    import re as _re
    runner = runner or subprocess.run
    notes = []
    for x in extras:
        url = x.get("url") or ""
        user = x.get("clone_user") or ""
        clone_url = (_re.sub(r"^(https?://)", rf"\g<1>{user}@", url)
                     if user else url)
        slug = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        env = {**os.environ, "DEVCAKE_FORGE_TOKEN": x.get("token") or ""}
        r = runner(["git", "clone", "--depth", "1", clone_url,
                    str(repo_dir / slug)],
                   capture_output=True, text=True, env=env)
        if r.returncode != 0:
            notes.append(f"extra repo {x.get('name', slug)}: clone failed "
                         f"({(r.stderr or '')[-200:]})")
        else:
            notes.append(f"extra repo {x.get('name', slug)}: cloned "
                         f"read-only at repo/{slug}")
    return notes

def clone_error_class(stderr: str) -> str:
    """DEV_FORGE_AUTH only on git's credential wording — a bare "403"/"401"
    can be a rate limit or an incidental URL fragment, and DEV_FORGE_AUTH
    latches the app's global forge breaker."""
    lowered = stderr.lower()
    auth_markers = ("returned error: 403", "returned error: 401",
                    "authentication failed", "repository not found",
                    "write access to repository not granted",
                    "could not read username", "could not read password",
                    "invalid credentials")
    return "DEV_FORGE_AUTH" if any(m in lowered for m in auth_markers) else "DEV_FORGE"
