"""Workspace forensics and result.json recovery (ADR-0018)."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import subprocess
from datetime import datetime, timezone

from devcake_dev.domain.fault import _one_line

WORKSPACE = pathlib.Path("/workspace")

FORENSIC_MAX_ENTRIES = 20
FORENSIC_STDERR_TAIL = 500
# An entry-count cap alone does not bound the payload: json.dumps escapes each
# non-ASCII character to \uXXXX, so 20 accented 100-char names serialize to ~12 KB.
# Budget the total instead.
FORENSIC_LISTING_BUDGET = 800


def workspace_forensics(out_dir, harness_exit, out_bytes=0, out_lines_n=0,
                        stderr_tail="") -> dict:
    """Cheap, bounded post-mortem shipped on EVERY failure artifact: three
    syscalls, no recursion, under ~1 KB. Answers what a human previously could
    not answer from the mission feed alone — did the harness die on a signal,
    was anything written, was the directory writable, was the disk full, and
    was the channel we classify on (stderr) empty."""
    info = {"harness_exit": harness_exit,
            "stdout_bytes": out_bytes, "stdout_lines": out_lines_n,
            "stderr_bytes": len(stderr_tail or "")}
    listing, err = [], None
    try:
        with os.scandir(out_dir) as it:
            entries = sorted(it, key=lambda e: e.name)
        spent = 0
        for i, entry in enumerate(entries):
            if i >= FORENSIC_MAX_ENTRIES or spent >= FORENSIC_LISTING_BUDGET:
                listing.append(f"+{len(entries) - i} more")
                break
            try:
                row = f"{entry.name[:100]}:{entry.stat().st_size}"
            except OSError as e:
                row = f"{entry.name[:100]}:?({e.errno})"
            listing.append(row)
            spent += len(json.dumps(row))      # escaped length, not raw length
    except OSError as e:
        err = f"{getattr(e, 'errno', '?')}: {e.strerror or e}"
    info["out_listing"] = listing
    info["out_error"] = err
    info["out_writable"] = bool(os.access(str(out_dir), os.W_OK))
    try:
        info["workspace_free_mb"] = shutil.disk_usage(str(WORKSPACE)).free // (1024 * 1024)
    except OSError:
        info["workspace_free_mb"] = None
    if stderr_tail:
        info["stderr_tail"] = _one_line(stderr_tail, FORENSIC_STDERR_TAIL)
    return info


def bad_output_reason(exc: BaseException) -> str:
    """Split the single blanket `except` on the result.json read into causes a
    human can act on. FileNotFoundError is tested before OSError (it is a
    subclass), JSONDecodeError before the generic fallback."""
    if isinstance(exc, FileNotFoundError):
        return "missing"
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_json"
    if isinstance(exc, AssertionError):
        return "illegal_outcome" if "outcome" in str(exc) else "bad_summary"
    if isinstance(exc, OSError):
        return "unreadable"
    return "invalid"


def _git_tracked(workdir, path, runner=None) -> bool:
    """True when `path` is tracked by the repo clone's git index. EXECUTE tells
    the Dev to commit at the end, so a stray result.json in the work tree may
    already have been swept into the PR — a different question from "was it
    written during this run", which the mtime gate answers."""
    runner = runner or subprocess.run
    try:
        rel = pathlib.Path(path).relative_to(workdir)
    except ValueError:
        return False
    try:
        r = runner(["git", "-C", str(workdir), "ls-files", "--error-unmatch", str(rel)],
                   capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — advisory annotation; a missing git must never fail a run
        return False
    return r.returncode == 0


def find_result_json(workspace, workdir, started_at: float, runner=None):
    """(path, note) — the canonical path first, then a FIXED candidate list.

    Deliberately no traversal: there is no depth parameter to get wrong and no
    way to enumerate the repo tree. A non-canonical hit is reported only when
    its mtime is at or after harness start, so a result.json that was already
    in the clone (a fixture, a project's own artifact) can never be adopted."""
    workspace, workdir = pathlib.Path(workspace), pathlib.Path(workdir)
    canonical = workspace / "out" / "result.json"
    try:
        # NOT `canonical.exists()`: on Python 3.12 (the pinned image base) that
        # swallows only ENOENT/ENOTDIR/EBADF/ELOOP and lets EACCES propagate —
        # an unreadable out/ would raise straight out of the recovery helper
        # that exists to enrich the failure path.
        if canonical.is_file():
            return canonical, ""
    except OSError:
        return canonical, ""            # unreadable: let the caller's read report it
    for cand in (workspace / "result.json", workspace / "repo" / "result.json",
                 workdir / "result.json", workdir / "out" / "result.json"):
        try:
            if cand.is_symlink():
                # `stat()` follows links, so both the freshness gate and the
                # content would come from the TARGET — a symlink is a way out of
                # the fixed candidate list and out of the workspace entirely.
                continue
            st = cand.stat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue                    # a directory named result.json is not one
        if st.st_mtime < started_at:
            continue                    # predates the harness — not this run's
        ts = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
        note = (f"[devcake] result.json is not at /workspace/out/result.json — found "
                f"one at {cand} (mtime {ts}). The playbook requires the canonical "
                f"path; fix the prompt.")
        if _git_tracked(workdir, cand, runner=runner):
            note += (" This file is ALSO tracked by git — check the PR for a stray "
                     "result.json.")
        return cand, note
    return None, ""

