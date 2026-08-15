"""H1/H2 ratchets: identity lives on HarnessDialect; ids come from HARNESSES.

AST + registry + Bake alignment — not a second hand-maintained id list.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

from devcake.harness import HARNESSES

_COMMON_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "images" / "common",
    Path("/srv/images/common"),
]
COMMON = next((p for p in _COMMON_CANDIDATES if p.is_dir()), None)

_BAKE_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "docker-bake.hcl",
    Path("/srv/docker-bake.hcl"),
]
BAKE = next((p for p in _BAKE_CANDIDATES if p.exists()), None)


def _dialects():
    assert COMMON is not None, (
        "images/common missing — bind it at /srv/images/common")
    root = str(COMMON)
    if root not in sys.path:
        sys.path.insert(0, root)
    from devcake_dev.harness.dialect import dialects
    return dialects()


def test_every_registry_id_has_a_dialect_and_the_unknown_id_fails_closed():
    table = _dialects()
    assert set(table) == set(HARNESSES)
    from devcake_dev.harness.dialect import get_dialect
    with pytest.raises(ValueError, match="unknown harness"):
        get_dialect("something-new")


def test_every_registry_id_is_a_bake_images_target():
    """docs/16 H2: a new HARNESSES key must also land in group images."""
    assert BAKE is not None, (
        "docker-bake.hcl missing — bind it at /srv/docker-bake.hcl")
    text = BAKE.read_text()
    match = re.search(
        r'group\s+"images"\s*\{\s*targets\s*=\s*\[([^\]]+)\]', text)
    assert match, "group images not found in docker-bake.hcl"
    targets = {part.strip().strip('"')
               for part in match.group(1).split(",") if part.strip()}
    assert set(HARNESSES) == targets - {"hello"}
    for name, h in HARNESSES.items():
        assert f'target "{name}"' in text, name
        assert h.image.startswith(f"devcake/dev-{name}:"), h.image


def _is_harness_eq_string(node: ast.Compare) -> bool:
    if not (isinstance(node.left, ast.Name) and node.left.id == "harness"):
        return False
    if not any(isinstance(op, ast.Eq) for op in node.ops):
        return False
    return any(isinstance(c, ast.Constant) and isinstance(c.value, str)
               for c in node.comparators)


def _is_harness_get_with_default(node: ast.Call) -> bool:
    """`.get(harness, render_claude)` is the old Claude fall-through."""
    if not (isinstance(node.func, ast.Attribute) and node.func.attr == "get"):
        return False
    if len(node.args) < 2:
        return False
    return isinstance(node.args[0], ast.Name) and node.args[0].id == "harness"


def test_images_common_has_no_harness_identity_branch():
    """Bans `if harness ==` and `.get(harness, default)` under images/common.
    Does not prove the capture rig or every membership test is gone."""
    assert COMMON is not None, (
        "images/common missing — bind it at /srv/images/common")
    offenders = []
    for path in sorted(COMMON.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = path.relative_to(COMMON)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and _is_harness_eq_string(node):
                offenders.append(f"{rel}:{node.lineno} if harness == …")
            elif isinstance(node, ast.Call) and _is_harness_get_with_default(node):
                offenders.append(f"{rel}:{node.lineno} .get(harness, default)")
    assert not offenders, (
        "identity branching leaked out of dialects.py (docs/16 H1):\n  "
        + "\n  ".join(offenders))
