#!/usr/bin/env python3
"""Digest-pin gate (docs/16 M8, ISSUES #29): every external base image —
Dockerfile ARG defaults / FROM lines / COPY --from refs and compose `image:`
values — must be pinned `tag@sha256:<64 hex>`. Local bake images (devcake/*)
and stage references are exempt; every devcake/* compose service must declare
`pull_policy: never` (otherwise a typo'd tag would pull from the public —
squattable — Docker Hub `devcake` namespace). Exit non-zero listing offenders.

Files are DISCOVERED (rglob Dockerfile*, glob docker-compose*.yml), not
hardcoded — a new Dockerfile or a docker-compose.override.yml (which compose
auto-loads) is scanned the moment it exists (audit A14).

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
_SKIP_PARTS = {".git", "node_modules", ".buildx-cache", "dist"}

PINNED = re.compile(r"@sha256:[0-9a-f]{64}\b")
LOCAL_PREFIX = "devcake/"


def _dockerfiles() -> list[Path]:
    return sorted(p for p in ROOT.rglob("Dockerfile*")
                  if not (_SKIP_PARTS & set(p.parts)) and p.is_file())


def _compose_files() -> list[Path]:
    return sorted(ROOT.glob("docker-compose*.yml")) + sorted(
        ROOT.glob("docker-compose*.yaml"))


def _strip_platform(tokens: list[str]) -> list[str]:
    """FROM/COPY may carry --platform=<x> (and COPY --chown/--chmod) before
    the ref — flags are not the base image (audit A14 false positive)."""
    return [t for t in tokens if not t.startswith("--")]


def check_dockerfile(path: Path, offenders: list[str]) -> None:
    """Every FROM and external COPY --from must resolve to a pinned ref:
    stage refs and bare stage indexes are local; a ${VAR} base is resolved
    against ANY collected ARG default (not just *_IMAGE-named ones — a
    rename must not slip past the gate)."""
    args: dict[str, str] = {}
    stages: set[str] = set()
    rel = path.relative_to(ROOT)
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        m = re.match(r"ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)", line)
        if m:
            args[m.group(1)] = m.group(2)
            continue
        m = re.match(r"COPY\s+(.*)", line, re.IGNORECASE)
        if m:
            src = re.search(r"--from=(\S+)", m.group(1))
            if src:
                ref = src.group(1)
                # a stage alias, a bare stage index, or a local bake image is
                # fine; anything else pulls an EXTERNAL image at build time
                # exactly like FROM and must be pinned (audit A14)
                if (ref not in stages and not ref.isdigit()
                        and not ref.startswith(LOCAL_PREFIX)
                        and not PINNED.search(ref)):
                    offenders.append(f"{rel}:{lineno}: {line}")
            continue
        m = re.match(r"FROM\s+(.+)", line, re.IGNORECASE)
        if not m:
            continue
        tokens = _strip_platform(m.group(1).split())
        if not tokens:
            continue
        base = tokens[0]
        alias = None
        if len(tokens) >= 3 and tokens[1].upper() == "AS":
            alias = tokens[2]
        if alias:
            stages.add(alias)
        if base in stages:
            continue                       # local stage reference
        var = re.fullmatch(r"\$\{(\w+)(?::-[^}]*)?\}", base)
        if var:
            base = args.get(var.group(1), "")
            if not base:
                offenders.append(
                    f"{rel}:{lineno}: FROM ${{{var.group(1)}}} "
                    f"has no in-file ARG default to verify")
                continue
        if not PINNED.search(base):
            offenders.append(f"{rel}:{lineno}: {line}")


def check_compose(path: Path, offenders: list[str]) -> None:
    """External images must be pinned; devcake/* images are exempt from
    pinning but MUST carry `pull_policy: never` in the same service block —
    the exemption otherwise trusts an unclaimed public registry namespace."""
    rel = path.relative_to(ROOT)
    lines = path.read_text().splitlines()
    for lineno, raw in enumerate(lines, 1):
        m = re.match(r"(\s*)image:\s*(\S+)", raw)
        if not m:
            continue
        indent, ref = m.group(1), m.group(2)
        # exact-boundary match: only local bake images (devcake/<name>) are
        # exempt — a registry image merely NAMED devcake-something is not
        if ref.startswith(LOCAL_PREFIX):
            if not _service_block_has(lines, lineno - 1, len(indent),
                                      "pull_policy: never"):
                offenders.append(
                    f"{rel}:{lineno}: {ref} is a local image without "
                    f"pull_policy: never — a missing local tag would pull "
                    f"from the public devcake/ Docker Hub namespace")
            continue
        if not PINNED.search(ref):
            offenders.append(f"{rel}:{lineno}: {raw.strip()}")


def _service_block_has(lines: list[str], image_idx: int, indent: int,
                       needle: str) -> bool:
    """Scan the service block around the image: line — sibling keys share the
    image line's indentation; the block ends at a shallower indent."""
    def _siblings(rng):
        for i in rng:
            raw = lines[i]
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            cur = len(raw) - len(raw.lstrip())
            if cur < indent:
                break                      # left the service block
            if cur == indent:
                yield raw.strip()
    for stripped in _siblings(range(image_idx + 1, len(lines))):
        if stripped.startswith(needle):
            return True
    for stripped in _siblings(range(image_idx - 1, -1, -1)):
        if stripped.startswith(needle):
            return True
    return False


def main() -> int:
    offenders: list[str] = []
    for df in _dockerfiles():
        check_dockerfile(df, offenders)
    for cf in _compose_files():
        check_compose(cf, offenders)
    if offenders:
        print("UNPINNED image references (ISSUES #29 — pin tag@sha256:…):")
        for o in offenders:
            print(f"  {o}")
        return 1
    print("image pins: all external references digest-pinned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
