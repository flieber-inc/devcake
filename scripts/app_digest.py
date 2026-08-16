#!/usr/bin/env python3
"""Content digest of the files receipts are keyed on.

The running app compares receipt.digest to its DEVCAKE_APP_DIGEST ARG.
This is not DEVCAKE_TAG. Bare `bake app` leaves the sentinel.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Paths whose change must invalidate receipts (harness images, probe, pins).
_TREES = ("images", "scripts/harness_probe", "app/devcake/house_pins.py")


def compute(root: Path | None = None) -> str:
    base = Path(root) if root is not None else ROOT
    digest = hashlib.sha256()
    files: list[Path] = []
    for rel in _TREES:
        p = base / rel
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files.extend(sorted(q for q in p.rglob("*")
                                if q.is_file() and "__pycache__" not in q.parts))
    for path in files:
        digest.update(path.relative_to(base).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> int:
    print(compute())
    return 0


if __name__ == "__main__":
    sys.exit(main())
