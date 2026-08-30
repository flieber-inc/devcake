"""Console entry for the ``devcake`` command.

Phase 1a (CAKE-176 / ADR-0038): only ``baker run`` is implemented. Other v1
verbs exit usage (2) until their sibling issues land. Universal ``--help`` /
``--json`` are accepted on the CLI surface; ``baker run`` is long-lived and
does not invent baker-specific flags beyond what ``dev_factory`` already
honors via env.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from . import baker


_USAGE = """\
usage: devcake [--json] <verb> …

Implemented:
  baker run     Host baker foreground / supervisor entry
                (same loop as python -m dev_factory)

Not yet implemented (ADR-0038 v1 — sibling issues):
  up, down, status, doctor, bake, setup

Install: uv tool install .   OR   pipx install .
Docs:    docs/adr/0038-devcake-cli-scope-command-surface-and-agent-operability.md
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devcake",
        description="DevCake host CLI",
        add_help=True,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON receipt on stdout (agent-operability; ADR-0038 Decision 2)",
    )
    sub = parser.add_subparsers(dest="verb")

    baker_parser = sub.add_parser(
        "baker",
        help="Host baker verbs",
    )
    baker_sub = baker_parser.add_subparsers(dest="baker_verb")
    baker_sub.add_parser(
        "run",
        help="Run the host baker loop (flock singleton, keep-set conveyor)",
    )

    # Register known-but-unimplemented verbs so `--help` lists them and
    # bare `devcake up` exits usage (2) instead of "invalid choice".
    for name, help_text in (
        ("up", "Bring up the compose stack (not yet implemented)"),
        ("down", "Stop the compose stack (not yet implemented)"),
        ("status", "Readiness snapshot (not yet implemented)"),
        ("doctor", "Preflight checks (not yet implemented)"),
        ("bake", "Image bake without starting the stack (not yet implemented)"),
        ("setup", "Operator/agent bootstrap (not yet implemented)"),
    ):
        sub.add_parser(name, help=help_text)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry. Returns a process exit code (0 / 2 / …)."""
    argv_list = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    try:
        args = parser.parse_args(argv_list)
    except SystemExit as exc:
        # argparse already printed help / usage; normalize None → 0.
        code = exc.code
        if code is None:
            return 0
        return int(code)

    if args.verb is None:
        sys.stderr.write(_USAGE)
        return 2

    if args.verb == "baker":
        if getattr(args, "baker_verb", None) != "run":
            sys.stderr.write("usage: devcake baker run\n")
            return 2
        # Long-lived: --json is accepted for surface uniformity but the
        # baker loop owns stdout after entry (no invented receipt schema).
        return baker.run()

    sys.stderr.write(
        f"devcake: '{args.verb}' is not implemented yet "
        f"(ADR-0038 phase 1a ships only 'baker run')\n"
        f"see: docs/adr/0038-devcake-cli-scope-command-surface-and-agent-operability.md\n"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
