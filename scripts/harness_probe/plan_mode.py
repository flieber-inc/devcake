"""Plan-mode row: compose production argv, then exec the binary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_FLAG_REJECT = (
    "unknown option",
    "unknown flag",
    "unrecognized option",
    "unrecognized arguments",
    "invalid option",
    "unexpected argument",
    "no such option",
)


def composed_plan_argv(template: str, prompt: str = "probe") -> list[str]:
    """Same harness_argv production uses, plan_mode=True."""
    _ensure_dialects()
    from devcake_dev.harness.argv import harness_argv
    return list(harness_argv(template, prompt, plan_mode=True, model="stub-model"))


def run_flag_accept(argv: list[str], *, timeout: float = 15.0,
                    env: dict | None = None) -> dict:
    """Exec composed argv. A flag reject is fail; anything else accepted."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError as exc:
        return {"error": f"binary missing: {exc}"}
    except subprocess.TimeoutExpired:
        # Hung after accepting the flag is not a flag-reject.
        return {"flag_accepted": True}
    text = f"{proc.stderr or ''}\n{proc.stdout or ''}".lower()
    if any(marker in text for marker in _FLAG_REJECT):
        return {"flag_accepted": False}
    return {"flag_accepted": True}


def _ensure_dialects() -> None:
    for candidate in (
        Path("/devcake_dev").parent,          # baked image layout
        Path("/"),                            # /devcake_dev at root
        Path("/srv/images/common"),           # pytest bind only for argv()
    ):
        if (candidate / "devcake_dev").is_dir():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return
    # Last resort: DEVCAKE_DEV_ROOT for tests that plant a tree.
    planted = os.environ.get("DEVCAKE_DEV_ROOT")
    if planted and Path(planted, "devcake_dev").is_dir():
        if planted not in sys.path:
            sys.path.insert(0, planted)
        return
    raise RuntimeError("devcake_dev not found — cannot compose plan-mode argv")
