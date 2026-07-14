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


def check_dockerfile(path: Path, offenders: list[str]) -> None:
    """Every FROM must resolve to a pinned ref: stage refs are local; a
    ${VAR} base is resolved against ANY collected ARG default (not just
    *_IMAGE-named ones — a rename must not slip past the gate)."""
    args: dict[str, str] = {}
    stages: set[str] = set()
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        m = re.match(r"ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)", line)
        if m:
            args[m.group(1)] = m.group(2)
            continue
        m = re.match(r"FROM\s+(\S+)(?:\s+AS\s+(\S+))?", line, re.IGNORECASE)
        if not m:
            continue
        base, alias = m.group(1), m.group(2)
        if alias:
            stages.add(alias)
        if base in stages:
            continue                       # local stage reference
        var = re.fullmatch(r"\$\{(\w+)(?::-[^}]*)?\}", base)
        if var:
            base = args.get(var.group(1), "")
            if not base:
                offenders.append(
                    f"{path.relative_to(ROOT)}:{lineno}: FROM ${{{var.group(1)}}} "
                    f"has no in-file ARG default to verify")
                continue
        if not PINNED.search(base):
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line}")


def check_compose(path: Path, offenders: list[str]) -> None:
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        m = re.match(r"\s*image:\s*(\S+)", raw)
        if not m:
            continue
        ref = m.group(1)
        # exact-boundary match: only local bake images (devcake/<name>) are
        # exempt — a registry image merely NAMED devcake-something is not
        if ref.startswith(LOCAL_PREFIX):
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
