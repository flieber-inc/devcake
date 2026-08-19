"""App safe_activity_relpath ↔ Dev _safe_activity_relpath pin (ADR-0034).

Cross-runtime copies (app process vs Dev image) cannot share an import; this
ratchet runs one shared input vector through both production functions so
zip-slip behavior drift turns red.
"""
from __future__ import annotations

import ast
from pathlib import Path

from devcake.domain.orchestrator.activity_payload import safe_activity_relpath

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[2]

ACTIVITY = Path("/srv/images/common/devcake_dev/workspace/activity.py")
if not ACTIVITY.exists():
    ACTIVITY = (_REPO / "images" / "common" / "devcake_dev"
                / "workspace" / "activity.py")


def _load_container_safe_relpath():
    assert ACTIVITY.exists(), (
        f"mount missing — pytest must bind images/common → /srv/images/common "
        f"(or run against the checkout); looked for {ACTIVITY}"
    )
    tree = ast.parse(ACTIVITY.read_text())
    keep = [n for n in tree.body
            if isinstance(n, ast.FunctionDef)
            and n.name == "_safe_activity_relpath"]
    assert keep, f"{ACTIVITY} lost _safe_activity_relpath"
    ns: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]),  # noqa: S102
                 str(ACTIVITY), "exec"), ns)
    return ns["_safe_activity_relpath"]


# Shared vectors: happy paths, empties, abs/~, .., nested .., backslash,
# depth > 20, segment > 200, "." segments, trailing slashes.
_VECTORS: tuple[str | object, ...] = (
    "a/b.md",
    "ok.md",
    "dir/sub/file.txt",
    "",
    None,
    123,
    "/abs",
    "/abs/nested",
    "~",
    "~/secret",
    "..",
    "../evil",
    "a/../../x",
    "a/../b",
    "a\\b\\c.md",
    "a/./b/./c.md",
    "a/b/",
    "a//b///c.md",
    "/".join(f"d{i}" for i in range(21)),
    "a/" + ("x" * 201),
    "a/" + ("x" * 200),
    ".",
    "./.",
    " ",
)


def test_safe_activity_relpath_parity_on_shared_vectors():
    container = _load_container_safe_relpath()
    for raw in _VECTORS:
        assert safe_activity_relpath(raw) == container(raw), (  # type: ignore[arg-type]
            f"zip-slip path drift on {raw!r}: "
            f"app={safe_activity_relpath(raw)!r} "
            f"container={container(raw)!r}"  # type: ignore[arg-type]
        )
