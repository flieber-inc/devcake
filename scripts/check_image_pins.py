#!/usr/bin/env python3
"""Digest-pin gate (docs/16 M8, ISSUES #29): every external base image —
Dockerfile ARG defaults / FROM lines and compose `image:` values — must be
pinned `tag@sha256:<64 hex>`. Local bake images (devcake/*) and stage
references are exempt. Exit non-zero listing offenders.

Bump procedure: docker buildx imagetools inspect <image:tag> → paste the
manifest-list digest.

KNOWN GAP, recorded not hidden: images/Dockerfile installs the Grok CLI via
`curl https://x.ai/cli/install.sh | bash` — a floating remote script inside a
digest-pinned image (x.ai ships no versioned artifact to pin as of 2026-07).
This checker covers image refs only; the exception lives here so it is
auditable.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILES = [ROOT / "app/Dockerfile", ROOT / "images/Dockerfile",
               ROOT / "admin/Dockerfile"]
COMPOSE = ROOT / "docker-compose.yml"

PINNED = re.compile(r"@sha256:[0-9a-f]{64}\b")
LOCAL_PREFIX = "devcake/"


def check_dockerfile(path: Path, offenders: list[str]) -> set[str]:
    stages: set[str] = set()
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        m = re.match(r"ARG\s+\w*_IMAGE=(\S+)", line)
        if m and not PINNED.search(m.group(1)):
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line}")
        m = re.match(r"FROM\s+(\S+)(?:\s+AS\s+(\S+))?", line, re.IGNORECASE)
        if m:
            base, alias = m.group(1), m.group(2)
            if alias:
                stages.add(alias)
            # ${VAR} bases resolve to (checked) ARG defaults; stage refs are local
            if base.startswith("${") or base in stages:
                continue
            if not PINNED.search(base):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line}")
    return stages


def check_compose(path: Path, offenders: list[str]) -> None:
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        m = re.match(r"\s*image:\s*(\S+)", raw)
        if not m:
            continue
        ref = m.group(1)
        if ref.startswith(LOCAL_PREFIX) or ref.startswith("devcake"):
            continue
        if not PINNED.search(ref):
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {raw.strip()}")


def main() -> int:
    offenders: list[str] = []
    for df in DOCKERFILES:
        check_dockerfile(df, offenders)
    check_compose(COMPOSE, offenders)
    if offenders:
        print("UNPINNED image references (ISSUES #29 — pin tag@sha256:…):")
        for o in offenders:
            print(f"  {o}")
        return 1
    print("image pins: all external references digest-pinned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
