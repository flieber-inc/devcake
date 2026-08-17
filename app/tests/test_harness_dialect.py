"""H1/H2 ratchets: identity lives on HarnessDialect; ids come from HARNESSES.

AST + registry + Bake alignment — not a second hand-maintained id list.
"""

import ast
import os
import re
import sys
from pathlib import Path

import pytest

from devcake.harness import HARNESSES

# dialects → render → bus reads these at import. Other entrypoint tests set
# them as a side effect; this file must stand alone (per-file / sharded runs).
os.environ.setdefault("DEVCAKE_RUN_ID", "test-harness-dialect")
os.environ.setdefault("REDIS_URL", "redis://localhost:6399/0")
os.environ.setdefault("REDIS_USER", "test")
os.environ.setdefault("REDIS_PASSWORD", "test")

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

_PUBLISH_CANDIDATES = [
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "docker-publish.yml",
    Path("/srv/docker-publish.yml"),
]
PUBLISH = next((p for p in _PUBLISH_CANDIDATES if p.exists()), None)

_COMPOSE_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "docker-compose.yml",
    Path("/srv/docker-compose.yml"),
]
COMPOSE = next((p for p in _COMPOSE_CANDIDATES if p.is_file()), None)

# Full images/ tree (not only common/hello binds) — Bake-only path.
_IMAGES_TREE_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "images",
    Path("/srv/images-tree"),
]
IMAGES_TREE = next((p for p in _IMAGES_TREE_CANDIDATES if p.is_dir()), None)


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


def test_every_all_target_has_ghcr_publish_remap():
    """group all + push:true must remap every harness onto ghcr.io, not Hub."""
    assert PUBLISH is not None, (
        "docker-publish.yml missing — bind it at /srv/docker-publish.yml")
    text = PUBLISH.read_text()
    for name in HARNESSES:
        assert f"{name}.tags=" in text, name


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


def test_compose_never_builds_devcake_images():
    """Bake-only: compose may pull/run third-party images, never build devcake/*."""
    assert COMPOSE is not None, (
        "docker-compose.yml missing — bind it at /srv/docker-compose.yml")
    text = COMPOSE.read_text()
    # No compose `build:` key at all today; if one appears later it must not
    # target a DevCake image (app / admin / dev-*). Hard fail on any build: —
    # resurrecting compose-built DevCake images is the regression this guards.
    assert re.search(r"(?m)^\s*build\s*:", text) is None, (
        "docker-compose.yml must not build images — use docker buildx bake "
        "(AGENTS.md / docs/13)")


def test_no_per_harness_dockerfile_under_images():
    """Single multi-target images/Dockerfile — no images/<harness>/Dockerfile."""
    assert IMAGES_TREE is not None, (
        "images/ tree missing — bind it at /srv/images-tree")
    root_dockerfile = IMAGES_TREE / "Dockerfile"
    assert root_dockerfile.is_file(), "images/Dockerfile is the only allowed Dockerfile"
    offenders = sorted(
        p.relative_to(IMAGES_TREE).as_posix()
        for p in IMAGES_TREE.rglob("Dockerfile")
        if p.resolve() != root_dockerfile.resolve()
    )
    assert not offenders, (
        "per-harness Dockerfiles are forbidden (Bake multi-target only):\n  "
        + "\n  ".join(offenders))
