#!/usr/bin/env python3
"""The Dev-container AppArmor profile must re-deny every path Docker masks
or freezes by default, because the run DAG launches Dev containers with
those masks removed (MaskedPaths/ReadonlyPaths empty — ADR-0023 addendum).
Docker has extended its lists before; this check asks the local daemon
what it masks TODAY and fails when the profile lacks a deny for any of it.

Runs where Docker is available (CI, an operator host); stdlib-only.
Usage: check_apparmor_masks.py [profile-path]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

PROFILE = Path(__file__).resolve().parents[1] / "scripts" / "apparmor" / "devcake-nested"
# digest-pinned like every other image reference in ops scripts (audit A14)
IMAGE = "alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"


def docker_lists() -> tuple[list[str], list[str], set[str]]:
    """Docker's masked and read-only lists for a default container, and
    which of those paths are directories (a read-only DIRECTORY needs the
    whole tree denied; a file needs only itself)."""
    cid = subprocess.run(["docker", "create", IMAGE, "true"], check=True,
                         capture_output=True, text=True).stdout.strip()
    try:
        out = subprocess.run(
            ["docker", "inspect", "--format",
             "{{json .HostConfig.MaskedPaths}}\n{{json .HostConfig.ReadonlyPaths}}", cid],
            check=True, capture_output=True, text=True).stdout.strip().splitlines()
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
    masked, readonly = json.loads(out[0]) or [], json.loads(out[1]) or []
    probe = " ".join(f'[ -d "{p}" ] && echo "{p}";' for p in readonly)
    dirs = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--security-opt", "systempaths=unconfined",
         IMAGE, "sh", "-c", probe], check=False, capture_output=True, text=True).stdout.split()
    return masked, readonly, set(dirs)


def _expand(pattern: str) -> list[str]:
    """Expand `{a,b}` alternations in an AppArmor path. Groups with an empty
    alternative (`{,**}`, `{,/**}`) are the tree-with-base idiom and stay
    literal for `_tree_base`."""
    m = re.search(r"\{([^{},]+(?:,[^{},]+)+)\}", pattern)
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


# Read-only trees Docker freezes that the profile denies only partly, on
# purpose (inherited from Docker's own profile): each entry names why.
PARTIAL_READONLY = {
    "/proc/sys": "Docker's own profile leaves /proc/sys/kernel/shm* writable "
                 "(POSIX shared memory sizing); everything else under /proc/sys "
                 "is denied by the class rules",
}


def _tree_base(rule_path: str) -> str | None:
    """The literal base of a rule that covers a whole tree INCLUDING the base
    itself (`X/{,**}` or `X{,/**}`); None for other shapes. `X/**` alone
    does not match X, so it is not a tree-with-base rule."""
    for suffix in ("/{,**}", "{,/**}"):
        if rule_path.endswith(suffix):
            base = rule_path[: -len(suffix)]
            return None if any(c in base for c in "*[?") else base
    return None


def _subtree_base(rule_path: str) -> str | None:
    """The literal base of a rule covering everything BELOW it (`X/**`)."""
    if rule_path.endswith("/**"):
        base = rule_path[:-3]
        return None if any(c in base for c in "*[?") else base
    return None


def _covered(path: str, rules: list[tuple[str, str]], need: str) -> bool:
    """A masked path (a file or a directory Docker hides): the profile must
    deny the path ITSELF — a literal rule, a `X/{,**}` tree rule at or above
    it, or a `X/**` subtree rule strictly above it. A single-level `/*` rule
    covers only its direct children."""
    for rule_path, perms in rules:
        if not set(need) <= set(perms):
            continue
        if rule_path == path:
            return True
        base = _tree_base(rule_path)
        if base is not None and (base == path or path.startswith(base + "/")):
            return True
        base = _subtree_base(rule_path)
        if base is not None and path.startswith(base + "/"):
            return True
        if rule_path.endswith("/*"):
            base = rule_path[:-2]
            if not any(c in base for c in "*[?") and path.rsplit("/", 1)[0] == base \
                    and path != base:
                return True
    return False


def _tree_covered(path: str, rules: list[tuple[str, str]], need: str,
                  is_dir: bool = True) -> bool:
    """A read-only entry: a FILE needs a rule for itself; a DIRECTORY needs
    the path itself AND everything below it write-denied — a `X/{,**}`
    rule at or above, or a `X/**` rule at or above plus a rule for the
    path itself."""
    if not is_dir:
        return _covered(path, rules, need)
    below = False
    for rule_path, perms in rules:
        if not set(need) <= set(perms):
            continue
        base = _tree_base(rule_path)
        if base is not None and (base == path or path.startswith(base + "/")):
            return True
        base = _subtree_base(rule_path)
        if base is not None and (base == path or path.startswith(base + "/")):
            below = True
    return below and _covered(path, rules, need)


def main(argv: list[str]) -> int:
    profile = Path(argv[1]) if len(argv) > 1 else PROFILE
    masked, readonly, dirs = docker_lists()
    rules = deny_rules(profile.read_text())
    problems = []
    for p in masked:
        if not _covered(p, rules, "r"):
            problems.append(f"masked by Docker but readable under the profile: {p}")
    for p in readonly:
        if _tree_covered(p, rules, "w", is_dir=p in dirs):
            continue
        if p in PARTIAL_READONLY and _covered(p, rules, "w"):
            print(f"check_apparmor_masks: {p} is denied partly, on purpose — "
                  f"{PARTIAL_READONLY[p]}")
            continue
        problems.append(f"read-only tree under Docker but not fully write-denied "
                        f"under the profile: {p}")
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
