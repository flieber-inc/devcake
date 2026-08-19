"""Repo clone helpers and forge clone-error classification."""
from __future__ import annotations

import os
import re
import subprocess


def inject_clone_user(url: str, user: str) -> str:
    """Embed ``user@`` after an http(s) scheme for forge clone URLs.

    Shared by the primary work-repo clone, activity clone, and sibling
    (extra/memory) clones so scheme coverage cannot drift. Empty user
    leaves the URL unchanged. Non-http(s) schemes are untouched.
    """
    if not user:
        return url
    return re.sub(r"^(https?://)", rf"\g<1>{user}@", url)


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
    scope). Dest: ``repo_dir / <url-slug>``. Failures are non-fatal."""
    return _clone_siblings(extras, dest_parent=repo_dir, dest_by="slug",
                           label="extra repo", rel_prefix="repo",
                           runner=runner)


def clone_memory_repos(mounts, memory_dir, runner=None, failures=None):
    """Consumer memory clones (PLAN_MEMORY §3.1). Dest:
    ``memory_dir / <card>`` — card name, not the URL slug. A failure is
    appended to `failures` (name/detail/mirror/entry) so the entrypoint
    can make a `strict` mount's failure fatal (§3.5); without the list
    the behavior stays non-fatal like extras."""
    memory_dir.mkdir(parents=True, exist_ok=True)
    return _clone_siblings(mounts, dest_parent=memory_dir, dest_by="card",
                           label="memory notebook", rel_prefix="memory",
                           runner=runner, failures=failures)


def _clone_siblings(entries, *, dest_parent, dest_by, label, rel_prefix,
                    runner=None, failures=None):
    """Shared mirror/askpass/LFS clone for extra and memory siblings."""
    from .provision import mirror_clone_argv, mirror_clone_env
    runner = runner or subprocess.run
    notes = []
    for x in entries:
        url = x.get("url") or ""
        user = x.get("clone_user") or ""
        mirror = x.get("mirror_path") or ""
        clone_url = inject_clone_user(url, user)
        slug = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        dest_name = (x.get("name") or slug) if dest_by == "card" else slug
        env = {**os.environ, "DEVCAKE_FORGE_TOKEN": x.get("token") or ""}
        dest = str(dest_parent / dest_name)
        if mirror:
            r = runner(mirror_clone_argv(mirror, dest, depth=1),
                       capture_output=True, text=True,
                       env=mirror_clone_env(os.environ))
        else:
            r = runner(["git", "clone", "--depth", "1", clone_url, dest],
                       capture_output=True, text=True, env=env)
        if r.returncode != 0:
            notes.append(f"{label} {x.get('name', dest_name)}: clone failed "
                         f"({(r.stderr or '')[-200:]})")
            if failures is not None:
                failures.append({"name": x.get("name", dest_name),
                                 "detail": (r.stderr or "")[-2000:],
                                 "mirror": bool(mirror), "entry": x})
            continue
        src = "mirror" if mirror else "forge"
        if mirror and clone_url:
            # best-effort origin rewrite — a failure leaves a workspace-only
            # oddity in a read-only clone, never worth failing the note over
            r2 = runner(set_origin_cmd(dest, clone_url),
                        capture_output=True, text=True, env=env)
            if r2.returncode != 0:
                notes.append(f"{label} {x.get('name', dest_name)}: origin "
                             f"rewrite failed ({(r2.stderr or '')[-120:]})")
        notes.append(f"{label} {x.get('name', dest_name)}: cloned "
                     f"read-only from {src} at {rel_prefix}/{dest_name}")
    return notes

def clone_error_class(stderr: str) -> str:
    """DEV_FORGE_AUTH only on git's credential wording — a bare "403"/"401"
    can be a rate limit or an incidental URL fragment, and DEV_FORGE_AUTH
    latches the app's global forge breaker.

    auth_markers are the Dev half of the app repo_mirror._AUTH_MARKERS pin
    (app omits "repository not found" — sync-path asymmetry). Guard:
    app/tests/test_mirror_auth_markers_pin.py (ADR-0034)."""
    lowered = stderr.lower()
    auth_markers = ("returned error: 403", "returned error: 401",
                    "authentication failed", "repository not found",
                    "write access to repository not granted",
                    "could not read username", "could not read password",
                    "invalid credentials")
    return "DEV_FORGE_AUTH" if any(m in lowered for m in auth_markers) else "DEV_FORGE"
