"""``devcake up --release [TAG]`` — check the release tag out before the
bring-up, with the guards a re-pin needs (docs/13; ADR-0038 addendum).

The tag resolution of ``up`` reads the checkout's ``VERSION``, so the
checkout happens first. Nothing is changed until every guard has passed:
a clean tree (modified tracked files refuse; untracked files are fine), a
CLI that is not running from inside this checkout (an editable install
would swap its own source mid-run), and a CLI at least as new as the one
the release ships (the tree's ``cli/devcake_cli/__init__.py``) — the
bring-up order that makes a re-pin safe lives in the CLI, so an older CLI
must not perform it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from . import __version__

RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
_VERSION_RE = re.compile(r'__version__\s*=\s*"([^"]+)"')


class ReleaseRefused(RuntimeError):
    """A guard refused the release checkout; the message names the remedy."""


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=str(root), text=True,
                          capture_output=True)
    if check and proc.returncode != 0:
        raise ReleaseRefused(
            f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def _vtuple(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version)[:3])


def release_tags(root: Path) -> list[str]:
    """Release tags of this checkout, newest first (``v*`` semver only —
    ``cli-v*`` and anything else are not releases)."""
    proc = _git(root, "tag", "--list", "v*", "--sort=-v:refname")
    return [t.strip() for t in proc.stdout.splitlines()
            if RELEASE_TAG_RE.match(t.strip())]


def resolve_release_tag(root: Path, wanted: str, *, fetch: bool = True) -> str:
    """``latest`` → the newest release tag after a tag fetch; a named tag
    must exist. Never a partial match."""
    if fetch:
        _git(root, "fetch", "--tags", "--quiet", "origin")
    tags = release_tags(root)
    if wanted in ("", "latest"):
        if not tags:
            raise ReleaseRefused("no release tag (v*) found — is origin reachable?")
        return tags[0]
    if not RELEASE_TAG_RE.match(wanted):
        raise ReleaseRefused(
            f"{wanted!r} is not a release tag (expected vX.Y.Z or 'latest')")
    if wanted not in tags:
        raise ReleaseRefused(f"release tag {wanted} not found after fetching tags")
    return wanted


def check_clean_tree(root: Path) -> None:
    proc = _git(root, "status", "--porcelain", "--untracked-files=no")
    dirty = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if dirty:
        raise ReleaseRefused(
            "the checkout has modified tracked files — commit, stash or "
            f"discard them first ({len(dirty)} file(s): "
            f"{', '.join(ln[3:] for ln in dirty[:3])}{'…' if len(dirty) > 3 else ''})")


def check_cli_not_editable_from(root: Path, cli_file: Path | None = None) -> None:
    here = (cli_file or Path(__file__)).resolve()
    try:
        here.relative_to(root.resolve())
    except ValueError:
        return
    raise ReleaseRefused(
        "this CLI runs from inside the checkout (editable install) — checking "
        "out another release would swap its own source mid-run; use a tool "
        "install (`uv tool install devcake-cli`) or check the tag out by hand")


def release_cli_version(root: Path, tag: str) -> str | None:
    """The CLI version the release ships, read from the tag without
    touching the working tree. None when the tag carries no CLI."""
    proc = _git(root, "show", f"{tag}:cli/devcake_cli/__init__.py", check=False)
    if proc.returncode != 0:
        return None
    m = _VERSION_RE.search(proc.stdout)
    return m.group(1) if m else None


def check_cli_matches_release(root: Path, tag: str,
                              running: str = __version__) -> None:
    shipped = release_cli_version(root, tag)
    if shipped and _vtuple(shipped) > _vtuple(running):
        raise ReleaseRefused(
            f"{tag} ships devcake-cli {shipped} but this CLI is {running}; "
            "the bring-up order that makes a re-pin safe lives in the CLI. "
            "Run: uv tool upgrade devcake-cli   (then re-run devcake up --release)")


def checkout_release(root: Path, wanted: str, *, dry_run: bool = False,
                     as_json: bool = False) -> str:
    """Resolve, guard, and check the release tag out (detached, exactly the
    manual recipe). Dry run resolves a named tag without fetching and
    changes nothing. Returns the tag."""
    def log(msg: str) -> None:
        if not as_json:
            sys.stdout.write(msg + "\n")
    check_cli_not_editable_from(root)
    if dry_run:
        if wanted in ("", "latest"):
            log("── would: git fetch --tags origin && git checkout <newest v* tag>")
            return "latest"
        tag = resolve_release_tag(root, wanted, fetch=False)
        log(f"── would: git fetch --tags origin && git checkout {tag}")
        return tag
    check_clean_tree(root)
    tag = resolve_release_tag(root, wanted)
    check_cli_matches_release(root, tag)
    _git(root, "checkout", "--quiet", tag)
    log(f"── checked out release {tag} (detached HEAD)")
    return tag
