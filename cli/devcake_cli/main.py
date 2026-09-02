"""Console entry for the ``devcake`` command.

Phase 1c (CAKE-178 / ADR-0038): ``baker run``, ``up``, ``down``, ``status``,
``doctor``, and ``setup``. ``bake`` remains a sibling stub (exit usage 2).
Universal ``--help`` / ``--json`` are accepted on the CLI surface.
"""

from __future__ import annotations

import sys
from typing import Sequence

from . import baker, doctor, down, setup, status, up


_USAGE = """\
usage: devcake [--json] <verb> …

Implemented:
  baker run     Host baker foreground / supervisor entry
                (same loop as python -m dev_factory)
  up            Bring up the compose stack (+ optional --bake)
  down          Stop the compose stack (no volume wipe)
  status        Compose + baker readiness snapshot
  doctor        Named preflight checks (+ remedies; --json)
  setup         First-setup / connections / settings-bundle import

Not yet implemented (ADR-0038 v1 — sibling issues):
  bake

Install: uv tool install devcake-cli   PyPI; upgrade: uv tool upgrade devcake-cli
         uv tool install .            this checkout (snapshot); re-run after git pull
         uv pip install -e .          editable venv; tracks the tree, nothing to re-run
Docs:    docs/adr/0038-devcake-cli-scope-command-surface-and-agent-operability.md
"""

_UP_HELP = """\
usage: devcake up [--bake [targets…]] [--dry-run] [--foreground-baker]
                  [--no-hello-smoke] [--] [service…]

Bring up the DevCake stack with discovered DOCKER_GID.
  --bake [targets…]     bake before up (default targets: app admin hello)
  --dry-run              print discovered GID + planned actions
  --foreground-baker     up, then run baker in foreground (no supervisor)
  --no-hello-smoke       with --bake, skip hello dispatch smoke
  [service…] or -- svc   optional compose service names
"""

_VERBS = frozenset(
    {"baker", "up", "down", "status", "doctor", "bake", "setup"}
)


def parse_up_flags(argv: Sequence[str]) -> up.UpOptions | int:
    """Parse ``up`` argv (flags, ``--bake`` targets, compose services).

    Returns ``UpOptions`` or an int exit code (0 for --help, 2 for usage).
    """
    dry_run = False
    foreground = False
    no_hello = False
    do_bake = False
    bake_targets: list[str] = []
    compose_args: list[str] = []
    tokens = list(argv)

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-h", "--help"):
            sys.stdout.write(_UP_HELP)
            return 0
        if tok == "--dry-run":
            dry_run = True
            i += 1
            continue
        if tok == "--foreground-baker":
            foreground = True
            i += 1
            continue
        if tok == "--no-hello-smoke":
            no_hello = True
            i += 1
            continue
        if tok == "--bake":
            do_bake = True
            i += 1
            while i < len(tokens) and not tokens[i].startswith("--"):
                bake_targets.append(tokens[i])
                i += 1
            continue
        if tok == "--":
            compose_args.extend(tokens[i + 1 :])
            break
        if tok.startswith("-"):
            sys.stderr.write(f"unknown option: {tok} (try --help)\n")
            return 2
        compose_args.extend(tokens[i:])
        break

    return up.UpOptions(
        bake=do_bake,
        bake_targets=bake_targets,
        dry_run=dry_run,
        foreground_baker=foreground,
        no_hello_smoke=no_hello,
        compose_services=compose_args,
        as_json=False,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry. Returns a process exit code (0 / 2 / 3 / 4 / 6 / …)."""
    argv_list = list(sys.argv[1:] if argv is None else argv)

    # Universal --json may appear before or after the verb (ADR-0038 Decision 2).
    as_json = "--json" in argv_list
    argv_list = [a for a in argv_list if a != "--json"]

    if not argv_list or argv_list[0] in ("-h", "--help"):
        if argv_list and argv_list[0] in ("-h", "--help"):
            sys.stdout.write(_USAGE)
            return 0
        sys.stderr.write(_USAGE)
        return 2

    verb = argv_list[0]
    rest = argv_list[1:]

    if verb not in _VERBS:
        sys.stderr.write(f"devcake: unknown verb {verb!r}\n")
        sys.stderr.write(_USAGE)
        return 2

    if verb == "baker":
        if rest == ["run"] or (rest and rest[0] == "run" and rest[1:] == []):
            return baker.run()
        if rest and rest[0] in ("-h", "--help"):
            sys.stdout.write("usage: devcake baker run\n")
            return 0
        sys.stderr.write("usage: devcake baker run\n")
        return 2

    if verb == "doctor":
        if rest and rest[0] in ("-h", "--help"):
            sys.stdout.write(
                "usage: devcake doctor [--json]\n"
                "Named preflight checks; prints one-time remedies. "
                "Never runs sudo/usermod/linger.\n"
            )
            return 0
        if rest:
            sys.stderr.write(f"devcake doctor: unknown option {rest[0]!r}\n")
            return 2
        return doctor.run_doctor(as_json=as_json)

    if verb == "down":
        if rest and rest[0] in ("-h", "--help"):
            sys.stdout.write(
                "usage: devcake down [--json]\n"
                "Stop the compose stack (docker compose down; never -v).\n"
            )
            return 0
        if rest:
            sys.stderr.write(f"devcake down: unknown option {rest[0]!r}\n")
            return 2
        return down.run_down(as_json=as_json)

    if verb == "status":
        if rest and rest[0] in ("-h", "--help"):
            sys.stdout.write(
                "usage: devcake status [--json]\n"
                "Compose project + baker liveness snapshot.\n"
            )
            return 0
        if rest:
            sys.stderr.write(f"devcake status: unknown option {rest[0]!r}\n")
            return 2
        return status.run_status(as_json=as_json)

    if verb == "up":
        parsed = parse_up_flags(rest)
        if isinstance(parsed, int):
            return parsed
        parsed.as_json = as_json
        return up.run_up(parsed)

    if verb == "setup":
        parsed = setup.parse_setup_flags(rest)
        if isinstance(parsed, int):
            return parsed
        parsed.as_json = as_json
        return setup.run_setup(parsed)

    # bake — registered but not yet implemented (sibling issue)
    if rest and rest[0] in ("-h", "--help"):
        sys.stdout.write(
            f"usage: devcake {verb}\n"
            f"(not yet implemented — ADR-0038 sibling issue)\n"
        )
        return 0
    sys.stderr.write(
        f"devcake: '{verb}' is not implemented yet "
        f"(ADR-0038 — see sibling issues)\n"
        f"see: docs/adr/0038-devcake-cli-scope-command-surface-and-agent-operability.md\n"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
