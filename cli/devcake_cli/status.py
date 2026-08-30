"""``devcake status`` — compose project + baker liveness snapshot."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .doctor import check_baker_liveness
from .paths import require_checkout_root


def _compose_ps(repo: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            cwd=str(repo),
            text=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        # Fallback without --format for older compose
        proc2 = subprocess.run(
            ["docker", "compose", "ps"],
            cwd=str(repo),
            text=True,
            capture_output=True,
            timeout=60,
        )
        text = (proc2.stdout or proc2.stderr or "").strip()
        return proc2.returncode == 0, text
    return True, (proc.stdout or "").strip()


def run_status(*, as_json: bool = False, repo: Path | None = None) -> int:
    try:
        root = repo or require_checkout_root()
    except FileNotFoundError as exc:
        sys.stderr.write(f"devcake status: {exc}\n")
        return 3

    compose_ok, compose_text = _compose_ps(root)
    baker = check_baker_liveness(repo_root=root)
    baker_alive = bool(baker.ok and "alive" in baker.detail and "dead" not in baker.detail)
    # Prefer explicit pid alive wording from check detail.
    if baker.detail.startswith("baker pid ") and "is alive" in baker.detail:
        baker_alive = True
    elif "not expected" in baker.detail or "skipped" in baker.detail:
        baker_alive = False

    payload = {
        "ok": compose_ok,
        "schema_version": 1,
        "compose_ok": compose_ok,
        "compose": compose_text,
        "baker_alive": baker_alive,
        "baker_detail": baker.detail,
        "checkout": str(root),
    }

    if as_json:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stdout.write(f"checkout: {root}\n")
        sys.stdout.write(f"compose: {'ok' if compose_ok else 'FAIL'}\n")
        if compose_text:
            sys.stdout.write(compose_text + "\n")
        sys.stdout.write(
            f"baker_alive: {baker_alive} ({baker.detail})\n"
        )
    return 0 if compose_ok else 4
