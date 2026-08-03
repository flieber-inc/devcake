"""Two-step run support (ADR-0025): phase selection, the app-written
sentinel, the provision-completion marker, and credential-stripped mirror
clone plumbing. Pure helpers — the entrypoint façade wires them."""
from __future__ import annotations

import pathlib

SENTINEL_REL = ".devcake/created-by-app"
MARKER_REL = ".devcake/provisioned"


def phase_of(environ) -> str:
    """DEVCAKE_PHASE → 'provision' | 'harness'; "" for anything else. The
    two-step dev-run DAG (ADR-0025) always sets one — there is no
    single-container fallback (rollback compat is not a pre-v1 concern), so
    the entrypoint crashes loudly on "" (a mismatched build / hand-run
    container)."""
    p = environ.get("DEVCAKE_PHASE", "")
    return p if p in ("provision", "harness") else ""


def _describe(workspace: pathlib.Path) -> str:
    """Forensics for sentinel/marker failures: enough to tell an operator-rm/
    daemon-recreate (root-owned, empty) from a half-finished provision."""
    try:
        st = workspace.stat()
        entries = sorted(p.name for p in workspace.iterdir())[:20]
        return (f"workspace owner uid={st.st_uid} "
                f"mode={oct(st.st_mode & 0o777)} entries={entries}")
    except OSError as e:
        return f"workspace unreadable: {type(e).__name__}: {e}"


def sentinel_error(workspace: pathlib.Path, run_id: str) -> str | None:
    """The app writes the run id into the sentinel at pre-create; a missing
    or mismatched sentinel means this container is bound to the WRONG
    directory (daemon autocreate of an absent bind source, or a stale/
    hand-edited DEVCAKE_WS_HOST pointing every step at the same wrong base)
    — fail loud, never provision into it."""
    p = workspace / SENTINEL_REL
    try:
        content = p.read_text().strip()
    except OSError as e:
        return (f"app sentinel {SENTINEL_REL} unreadable "
                f"({type(e).__name__}: {e}) — {_describe(workspace)}")
    if content != run_id:
        return (f"app sentinel names run {content!r}, expected {run_id!r} — "
                f"{_describe(workspace)}")
    return None


def marker_error(workspace: pathlib.Path, run_id: str) -> str | None:
    """The harness step must find a marker the provision step wrote for THIS
    run — anything else is a half-finished provision or the wrong directory."""
    p = workspace / MARKER_REL
    try:
        content = p.read_text().strip()
    except OSError as e:
        return (f"provision marker {MARKER_REL} missing "
                f"({type(e).__name__}: {e}) — {_describe(workspace)}")
    if content != run_id:
        return (f"provision marker names run {content!r}, expected {run_id!r} "
                f"— {_describe(workspace)}")
    return None


def mirror_clone_env(environ) -> dict:
    """A file:// mirror clone needs NO credential — strip the askpass and
    token so a repo-committed `.lfsconfig` cannot steer git-lfs at a token-
    echoing helper (ADR-0025 R3). Returns a copy; ambient env is untouched."""
    env = {k: v for k, v in dict(environ).items()
           if k not in ("GIT_ASKPASS", "DEVCAKE_FORGE_TOKEN")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def mirror_clone_argv(mirror_path: str, dest: str,
                      *, depth: int | None = None) -> list:
    """`-c lfs.url=` pins the LFS endpoint to the clone's OWN mirror:
    repo-local config outranks a committed `.lfsconfig`, so repo content
    cannot redirect the standalone transfer at another repo's mirror or an
    external host (ADR-0025 R3). `file://` (never the bare path) stays
    LOAD-BEARING (ADR-0024): plain-path local clones ignore --depth and try
    hardlinks; file:// forces the smart transport, which honors depth and
    carries LFS content via git-lfs's standalone file transfer."""
    argv = ["git", "clone", "-c", f"lfs.url=file://{mirror_path}"]
    if depth:
        argv += ["--depth", str(depth)]
    argv += [f"file://{mirror_path}", dest]
    return argv
