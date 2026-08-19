"""argv.forge_dialect is a dead duplicate of dev_entrypoint.forge_dialect
(ADR-0034). The entrypoint defines and calls its own; it never imports
forge_dialect from argv. This ratchet keeps the dead symbol from returning.
"""
from __future__ import annotations

import ast
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[2]

ARGV = Path("/srv/images/common/devcake_dev/harness/argv.py")
if not ARGV.exists():
    ARGV = _REPO / "images" / "common" / "devcake_dev" / "harness" / "argv.py"

ENTRYPOINT = Path("/srv/images/common/dev_entrypoint.py")
if not ENTRYPOINT.exists():
    ENTRYPOINT = _REPO / "images" / "common" / "dev_entrypoint.py"


def test_argv_does_not_define_forge_dialect():
    assert ARGV.exists(), (
        f"mount missing — pytest must bind images/common → /srv/images/common "
        f"(or run against the checkout); looked for {ARGV}"
    )
    tree = ast.parse(ARGV.read_text())
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "forge_dialect" not in names, (
        "images/common/devcake_dev/harness/argv.py still defines "
        "forge_dialect — dead duplicate of dev_entrypoint.forge_dialect; "
        "delete it (CAKE-85 / ADR-0034)"
    )


def test_entrypoint_still_defines_forge_dialect():
    assert ENTRYPOINT.exists(), f"missing {ENTRYPOINT}"
    tree = ast.parse(ENTRYPOINT.read_text())
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "forge_dialect" in names, (
        "dev_entrypoint.forge_dialect vanished — that is the live chokepoint"
    )
