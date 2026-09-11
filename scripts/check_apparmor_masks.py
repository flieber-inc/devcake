#!/usr/bin/env python3
"""The Dev-container AppArmor profile must re-deny every path Docker masks
or freezes by default, because the run DAG launches Dev containers with
those masks removed (MaskedPaths/ReadonlyPaths empty — ADR-0023 addendum).
Docker has extended its lists before; this check asks the local daemon
what it masks TODAY and fails when the profile lacks a deny for any of it.

Runs where Docker is available (CI, an operator host); stdlib-only.
Usage: check_apparmor_masks.py [profile-path] [image]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

PROFILE = Path(__file__).resolve().parents[1] / "scripts" / "apparmor" / "devcake-nested"
IMAGE = "alpine:3.20"


def docker_lists(image: str) -> tuple[list[str], list[str]]:
    cid = subprocess.run(["docker", "create", image, "true"], check=True,
                         capture_output=True, text=True).stdout.strip()
    try:
        out = subprocess.run(
            ["docker", "inspect", "--format",
             "{{json .HostConfig.MaskedPaths}}\n{{json .HostConfig.ReadonlyPaths}}", cid],
            check=True, capture_output=True, text=True).stdout.strip().splitlines()
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
    return json.loads(out[0]) or [], json.loads(out[1]) or []


def _expand(pattern: str) -> list[str]:
    """Expand one level of `{a,b}` alternation in an AppArmor path."""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    out = []
    for alt in m.group(1).split(","):
        out.extend(_expand(pattern[:m.start()] + alt + pattern[m.end():]))
    return out


def deny_rules(profile_text: str) -> list[tuple[str, str]]:
    """(path, permissions) for every `deny <path> <perms>,` rule."""
    rules = []
    for line in profile_text.splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"deny\s+(\S+)\s+([rwklxm]+)\s*,", line)
        if not m:
            continue
        path = m.group(1).replace("@{PROC}", "/proc")
        for p in _expand(path):
            rules.append((p, m.group(2)))
    return rules


def _covered(path: str, rules: list[tuple[str, str]], need: str) -> bool:
    """A deny rule covers `path` when its pattern is the path itself, the
    path plus a trailing tree glob, or the path's parent tree, with every
    permission in `need`."""
    for rule_path, perms in rules:
        if not set(need) <= set(perms):
            continue
        base = rule_path
        for suffix in ("/**", "/{,**}", "{,/**}", "/*"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        if rule_path == path or base == path:
            return True
        # a parent-tree rule such as /sys/[^f]*/** does not cover /sys/firmware;
        # only literal parents count
        if "*" not in base and "[" not in base and path.startswith(base + "/"):
            return True
    return False


def main(argv: list[str]) -> int:
    profile = Path(argv[1]) if len(argv) > 1 else PROFILE
    image = argv[2] if len(argv) > 2 else IMAGE
    masked, readonly = docker_lists(image)
    rules = deny_rules(profile.read_text())
    problems = []
    for p in masked:
        if not _covered(p, rules, "r"):
            problems.append(f"masked by Docker but readable under the profile: {p}")
    for p in readonly:
        if not _covered(p, rules, "w"):
            problems.append(f"read-only under Docker but writable under the profile: {p}")
    if problems:
        print("check_apparmor_masks: the profile no longer covers Docker's default masks:",
              file=sys.stderr)
        for line in problems:
            print("  " + line, file=sys.stderr)
        return 1
    print(f"check_apparmor_masks: {len(masked)} masked + {len(readonly)} read-only "
          f"paths all denied by {profile.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
