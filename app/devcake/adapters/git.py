"""Async git subprocess runner — the ONLY subprocess site in the app.

First subprocess use in `app/devcake`, deliberate and singular (ADR-0024):
the repo mirror needs real git; everything else in the app stays HTTP.
Minimal child env by construction — never `os.environ` — so no credential
can be inherited by accident; the caller passes exactly what a command
needs (GIT_ASKPASS + its token variable, nothing else).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("devcake.git")

GIT_TIMEOUT_SECONDS = 900   # bounds a wedged fetch; also the cold-clone budget

# The child sees ONLY this plus the caller's overlay. PATH is required for
# git to find its own helpers (git-remote-https, git-lfs).
_BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/home/app",
    "GIT_TERMINAL_PROMPT": "0",
    "LANG": "C.UTF-8",
}


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    # verbatim stdout bytes — file-content reads (`git show`) must not ride
    # the lossy text decode (ADR-0016 addendum: skill payloads are base64)
    raw_stdout: bytes = b""


async def run_git(args: list[str], *, cwd: Path | None = None,
                  env: dict[str, str] | None = None,
                  timeout: float = GIT_TIMEOUT_SECONDS) -> GitResult:
    """Run one git command. NEVER raises: a spawn failure (git missing from
    the image) returns rc=127 with the cause in stderr; a timeout kills the
    child and returns rc=124 with timed_out=True. Output decoded lossily —
    forge stderr is diagnostic text, not data."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            env={**_BASE_ENV, **(env or {})},
        )
    except Exception as e:  # noqa: BLE001 — a missing binary must gate, not crash the app
        return GitResult(127, "", f"git spawn failed: {type(e).__name__}: {e}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        # a cancelled caller (a bounded bulk probe past its deadline) must
        # not leave git running: kill, then re-raise the cancellation
        proc.kill()
        raise
    except TimeoutError:
        proc.kill()
        # reap; communicate after kill returns promptly
        try:
            await asyncio.wait_for(proc.communicate(), timeout=10)
        except Exception:  # noqa: BLE001 — unreapable child; the kill already landed
            pass
        return GitResult(124, "", f"git {' '.join(args[:2])} timed out "
                                  f"after {timeout:.0f}s", timed_out=True)
    return GitResult(proc.returncode or 0,
                     out.decode("utf-8", "replace"),
                     err.decode("utf-8", "replace"),
                     raw_stdout=out)
