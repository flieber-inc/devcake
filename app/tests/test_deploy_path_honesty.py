"""Deploy-path honesty guards (CAKE-41): Bake matrix + DAG pull policy.

Public seam: operator-facing docs must match docker-bake.hcl group images and
the live dev-run.yaml pull posture. Independent expected values come from the
Bake HCL and the DAG file — docs may not lag them.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_BAKE = next(
    (p for p in (_ROOT / "docker-bake.hcl", Path("/srv/docker-bake.hcl"))
     if p.is_file()),
    None,
)
_DOCS13 = next(
    (p for p in (
        _ROOT / "docs" / "13-deployment.md",
        Path("/srv/docs/13-deployment.md"),
    ) if p.is_file()),
    None,
)
_DOCS08 = next(
    (p for p in (
        _ROOT / "docs" / "08-harness-templates.md",
        Path("/srv/docs/08-harness-templates.md"),
    ) if p.is_file()),
    None,
)
_DAG = next(
    (p for p in (
        _ROOT / "dagu" / "dags" / "dev-run.yaml",
        Path("/srv/dagu-dags/dev-run.yaml"),
    ) if p.is_file()),
    None,
)
_COMPOSE = next(
    (p for p in (_ROOT / "docker-compose.yml", Path("/srv/docker-compose.yml"))
     if p.is_file()),
    None,
)


def _images_targets(bake_text: str) -> set[str]:
    match = re.search(
        r'group\s+"images"\s*\{\s*targets\s*=\s*\[([^\]]+)\]', bake_text)
    assert match, "group images not found in docker-bake.hcl"
    return {part.strip().strip('"')
            for part in match.group(1).split(",") if part.strip()}


def test_docs13_bake_matrix_lists_every_images_target():
    assert _BAKE is not None, "docker-bake.hcl missing"
    assert _DOCS13 is not None, "docs/13-deployment.md missing — bind /srv/docs"
    targets = _images_targets(_BAKE.read_text())
    doc = _DOCS13.read_text()
    assert "all three harnesses" not in doc, (
        "docs/13 still says three harnesses; Bake group images is larger")
    for name in sorted(targets):
        # Table rows / prose must name each baked image (hello + harnesses).
        needle = f"devcake/dev-{name}" if name != "hello" else "devcake/dev-hello"
        assert needle in doc, f"docs/13 bake matrix missing {needle}"


def test_docs_agree_with_live_dag_pull_never():
    """Stale 'pull_policy: missing' contradicts dagu/dags/dev-run.yaml."""
    assert _DAG is not None, "dev-run.yaml missing — bind /srv/dagu-dags"
    dag = _DAG.read_text()
    assert re.search(r"^\s*pull:\s*never\s*$", dag, re.M), (
        "live DAG must use pull: never")
    assert "pull_policy: missing" not in dag

    for path in (_DOCS13, _DOCS08):
        if path is None:
            continue
        text = path.read_text()
        assert "pull_policy: missing" not in text, (
            f"{path.name} still claims DAG pull_policy: missing")


def test_compose_never_builds_devcake_images():
    """Bake-only contract: compose has no build: keys; app/admin never-pull."""
    if _COMPOSE is None:
        # CI app-test does not always bind compose; skip rather than invent.
        return
    text = _COMPOSE.read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not re.match(r"build\s*:", stripped), (
            f"compose must not build images: {stripped!r}")
    assert "pull_policy: never" in text
