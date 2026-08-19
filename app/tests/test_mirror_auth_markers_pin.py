"""App repo_mirror._AUTH_MARKERS ↔ Dev clone_error_class auth_markers pin
(ADR-0034). Cross-runtime copies cannot share an import; this ratchet
compares the production tuples field-by-field so drift turns red.

Authoritative relation (repo_mirror.py): app markers == Dev markers minus
"repository not found" — on the mirror sync path a 404 is config/delete,
not a token failure, so the breaker must not latch as auth.
"""
from __future__ import annotations

import ast
from pathlib import Path

from devcake.domain import repo_mirror as _repo_mirror
from devcake.domain.repo_mirror import sync_error_class

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[2]

CLONE = Path("/srv/images/common/devcake_dev/workspace/clone.py")
if not CLONE.exists():
    CLONE = _REPO / "images" / "common" / "devcake_dev" / "workspace" / "clone.py"

REPO_MIRROR = Path(_repo_mirror.__file__)

_REPO_NOT_FOUND = "repository not found"


def _tuple_literals(path: Path, *, assign_name: str | None = None,
                    in_function: str | None = None) -> tuple[str, ...]:
    assert path.exists(), (
        f"mount missing — pytest must bind images/common → /srv/images/common "
        f"(or run against the checkout); looked for {path}"
    )
    tree = ast.parse(path.read_text())
    nodes = tree.body
    if in_function:
        fn = next(
            (n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name == in_function),
            None,
        )
        assert fn is not None, f"{path} lost function {in_function}"
        nodes = fn.body
    for node in nodes:
        if not isinstance(node, ast.Assign):
            continue
        if assign_name is not None:
            if not any(isinstance(t, ast.Name) and t.id == assign_name
                       for t in node.targets):
                continue
        if not isinstance(node.value, ast.Tuple):
            continue
        vals = []
        for elt in node.value.elts:
            assert isinstance(elt, ast.Constant) and isinstance(elt.value, str), (
                f"unexpected marker element in {path}: {ast.dump(elt)}"
            )
            vals.append(elt.value)
        return tuple(vals)
    raise AssertionError(
        f"no string-tuple assign "
        f"{assign_name or '(any)'} found in {path}"
        + (f"::{in_function}" if in_function else "")
    )


def _extract_clone_error_class(path: Path):
    """Load the pure clone_error_class function without importing the module
    (clone.py pulls subprocess / workspace helpers at module level)."""
    tree = ast.parse(path.read_text())
    keep = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "clone_error_class"]
    assert keep, f"{path} lost clone_error_class"
    ns: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]),  # noqa: S102
                 str(path), "exec"), ns)
    return ns["clone_error_class"]


def test_auth_markers_app_is_dev_minus_repository_not_found():
    app = _tuple_literals(REPO_MIRROR, assign_name="_AUTH_MARKERS")
    dev = _tuple_literals(CLONE, assign_name="auth_markers",
                          in_function="clone_error_class")
    assert set(app) == set(dev) - {_REPO_NOT_FOUND}, (
        f"auth-marker drift: app={sorted(app)!r} "
        f"dev={sorted(dev)!r} — expected app == dev - {{{_REPO_NOT_FOUND!r}}}"
    )
    assert _REPO_NOT_FOUND in set(dev)
    assert _REPO_NOT_FOUND not in set(app)


def test_auth_marker_asymmetry_on_shared_stderr_vectors():
    """Both sides agree on credential wording; only Dev treats a missing
    repo as auth (app keeps it transient — breaker copy would mislead)."""
    clone_error_class = _extract_clone_error_class(CLONE)
    agree_auth = (
        "remote: Authentication failed",
        "fatal: could not read Username for 'https://example.com'",
        "fatal: could not read Password for 'https://example.com'",
        "remote: Invalid credentials",
        "remote: Write access to repository not granted",
        "The requested URL returned error: 403",
        "The requested URL returned error: 401",
    )
    for stderr in agree_auth:
        assert sync_error_class(stderr) == "auth", stderr
        assert clone_error_class(stderr) == "DEV_FORGE_AUTH", stderr

    assert sync_error_class("ERROR: Repository not found.") == "transient"
    assert clone_error_class("ERROR: Repository not found.") == "DEV_FORGE_AUTH"

    assert sync_error_class("fatal: unable to access") == "transient"
    assert clone_error_class("fatal: unable to access") == "DEV_FORGE"
