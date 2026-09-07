#!/usr/bin/env python3
"""The committed release pin and the changelog must agree (a CI gate).

`VERSION` at the checkout root is the tag `devcake up` bakes and runs images
under (docs/13); the newest `## vX.Y.Z (date)` section of CHANGELOG.md is the
release the notes describe. A release is cut by bumping both in one change,
so this check refuses a drift in either direction. Silent on success; exit 1
with both values otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

RELEASE_HEADING = re.compile(r"^## (v\d+\.\d+\.\d+) \(", re.MULTILINE)
PIN = re.compile(r"v\d+\.\d+\.\d+")


def newest_release(changelog: str) -> str | None:
    """The first release heading in the changelog (newest first by convention)."""
    m = RELEASE_HEADING.search(changelog)
    return m.group(1) if m else None


def check(version: str, changelog: str) -> str | None:
    """None when the pin and the newest changelog release agree, else why not."""
    pin = version.strip()
    if not PIN.fullmatch(pin):
        return f"VERSION must be a release tag such as v0.5.9, got {pin!r}"
    newest = newest_release(changelog)
    if newest is None:
        return "CHANGELOG.md has no release section (## vX.Y.Z (date))"
    if pin != newest:
        return (f"VERSION pins {pin} but the newest CHANGELOG.md release is "
                f"{newest} — a release bumps both in one change")
    return None


def main(root: Path | None = None) -> int:
    root = root or Path(__file__).resolve().parent.parent
    err = check((root / "VERSION").read_text(encoding="utf-8"),
                (root / "CHANGELOG.md").read_text(encoding="utf-8"))
    if err:
        print(err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
