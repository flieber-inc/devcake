"""``devcake down`` — ``docker compose down`` without ``-v`` (ADR-0038)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .paths import require_checkout_root


def run_down(*, as_json: bool = False, repo: Path | None = None) -> int:
    try:
        root = repo or require_checkout_root()
    except FileNotFoundError as exc:
        sys.stderr.write(f"devcake down: {exc}\n")
        return 3

    argv = ["docker", "compose", "down"]
    # Never pass -v in v1 (ADR-0038 Decision 1).
    assert "-v" not in argv
    if not as_json:
        sys.stdout.write("── docker compose down\n")
    proc = subprocess.run(argv, cwd=str(root))
    ok = proc.returncode == 0
    if as_json:
        payload = {
            "ok": ok,
            "schema_version": 1,
            "argv": argv,
            "volumes_removed": False,
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    if not ok:
        return 4
    return 0
