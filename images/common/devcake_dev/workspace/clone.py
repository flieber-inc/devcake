"""Repo clone helpers and forge clone-error classification."""
from __future__ import annotations

import os
import subprocess


def clone_source(mirror_path: str, clone_url: str) -> str:
    """Where a clone reads from (ADR-0024): the read-only mirror when the
    app served one, else the authenticated forge URL. `file://` (never the
    bare path) is LOAD-BEARING: plain-path local clones ignore --depth and
    try hardlinks; file:// forces the smart transport, which honors depth
    and carries LFS content via git-lfs's standalone file transfer."""
    return f"file://{mirror_path}" if mirror_path else clone_url


def set_origin_cmd(dest: str, clone_url: str) -> list:
    """Post-mirror-clone origin rewrite: the workspace's remote must be the
    REAL forge (clone_user@ form — askpass supplies the token, docs/03 §3)
    so push/PR/mid-run fetch behave exactly as a direct clone."""
    return ["git", "-C", dest, "remote", "set-url", "origin", clone_url]


def mirror_clone_error_class(stderr: str) -> str:  # noqa: ARG001 — signature parity with clone_error_class
    """A file:// clone from the mounted mirror can NEVER be a forge-
    credential failure — always DEV_FORGE (bounded excusals), never
    DEV_FORGE_AUTH, which would latch the repo's breaker over
    infrastructure trouble (missing volume, corrupt pack)."""
    return "DEV_FORGE"


def clone_extra_repos(extras, repo_dir, runner=None):
    """Read-only sibling clones for multi-repo ONBOARD triage (item 2 full
    scope). Mirrored entries (ADR-0024: `mirror_path`, no token) clone from
    the RO volume and get their origin rewritten to the real URL; direct
    entries ride their OWN read token via the shared askpass script
    (per-clone env override). Shallow (--depth 1) — assessment only.
    A failed extra clone is deliberately NON-fatal: triage proceeds on what
    cloned, and the failures are returned for the transcript/log."""
    import re as _re
    runner = runner or subprocess.run
    notes = []
    for x in extras:
        url = x.get("url") or ""
        user = x.get("clone_user") or ""
        mirror = x.get("mirror_path") or ""
        clone_url = (_re.sub(r"^(https?://)", rf"\g<1>{user}@", url)
                     if user else url)
        slug = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        env = {**os.environ, "DEVCAKE_FORGE_TOKEN": x.get("token") or ""}
        dest = str(repo_dir / slug)
        r = runner(["git", "clone", "--depth", "1",
                    clone_source(mirror, clone_url), dest],
                   capture_output=True, text=True, env=env)
        if r.returncode != 0:
            notes.append(f"extra repo {x.get('name', slug)}: clone failed "
                         f"({(r.stderr or '')[-200:]})")
            continue
        src = "mirror" if mirror else "forge"
        if mirror and clone_url:
            # best-effort origin rewrite — a failure leaves a workspace-only
            # oddity in a read-only clone, never worth failing the note over
            r2 = runner(set_origin_cmd(dest, clone_url),
                        capture_output=True, text=True, env=env)
            if r2.returncode != 0:
                notes.append(f"extra repo {x.get('name', slug)}: origin "
                             f"rewrite failed ({(r2.stderr or '')[-120:]})")
        notes.append(f"extra repo {x.get('name', slug)}: cloned "
                     f"read-only from {src} at repo/{slug}")
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
