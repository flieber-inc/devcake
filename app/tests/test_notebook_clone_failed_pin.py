"""Entrypoint strict-memory clone_failed detail ↔ finalize parser pin (ADR-0034).

The Dev container composes ``memory notebook {name} clone failed: {detail}``;
the app's ``notebook_card_from_forge_auth_detail`` parses the card out of that
string. Silent wording drift falls through to the work-repo latch (CAKE-60).
Cross-runtime copies cannot share an import; this ratchet AST-extracts the
composer f-string and asserts the live parser still reads the card name.
"""
from __future__ import annotations

import ast
from pathlib import Path

from devcake.domain.orchestrator.finalize import notebook_card_from_forge_auth_detail

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[2]

ENTRYPOINT = Path("/srv/images/common/dev_entrypoint.py")
if not ENTRYPOINT.exists():
    ENTRYPOINT = _REPO / "images" / "common" / "dev_entrypoint.py"


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _f_subscript_key(node: ast.FormattedValue) -> str | None:
    """Return the string key when value is ``f['…']``, else None."""
    val = node.value
    if not (isinstance(val, ast.Subscript)
            and isinstance(val.value, ast.Name)
            and val.value.id == "f"
            and isinstance(val.slice, ast.Constant)
            and isinstance(val.slice.value, str)):
        return None
    return val.slice.value


def _joined_str_template(joined: ast.JoinedStr) -> str:
    """Rebuild a ``str.format`` template from JoinedStr parts (composer side)."""
    parts: list[str] = []
    for v in joined.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            parts.append(v.value)
            continue
        if isinstance(v, ast.FormattedValue):
            key = _f_subscript_key(v)
            assert key is not None, (
                f"unexpected FormattedValue in clone_failed JoinedStr: "
                f"{ast.dump(v)}"
            )
            parts.append("{" + key + "}")
            continue
        raise AssertionError(
            f"unexpected JoinedStr part in clone_failed: {ast.dump(v)}"
        )
    return "".join(parts)


def _entrypoint_notebook_clone_failed_template() -> str:
    assert ENTRYPOINT.exists(), (
        f"mount missing — pytest must bind images/common → /srv/images/common "
        f"(or run against the checkout); looked for {ENTRYPOINT}"
    )
    tree = ast.parse(ENTRYPOINT.read_text())
    hits: list[ast.JoinedStr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) != "clone_failed" or not node.args:
            continue
        arg0 = node.args[0]
        if not isinstance(arg0, ast.JoinedStr):
            continue
        consts = "".join(
            v.value for v in arg0.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
        if "memory notebook " in consts and " clone failed" in consts:
            hits.append(arg0)
    assert len(hits) == 1, (
        f"expected exactly one clone_failed JoinedStr for memory notebook "
        f"failures in {ENTRYPOINT}; found {len(hits)}"
    )
    return _joined_str_template(hits[0])


def test_notebook_clone_failed_detail_pins_parser_card_extraction():
    """Composer f-string and finalize regex must agree on the card slot."""
    template = _entrypoint_notebook_clone_failed_template()
    card = "nb-card"
    composed = template.format(
        name=card,
        detail="remote: Authentication failed",
    )
    assert notebook_card_from_forge_auth_detail(composed) == card
