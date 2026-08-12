"""Dispatch chokepoint tripwire.

Every Dev ACL user create + executor.start MUST go through
``RunBootstrap.launch`` so clear-runs can serialize on ``dispatch_lock``.
PR #30 claimed "poll is the only dispatcher" — false. This AST scan makes
that class of claim fail CI.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "devcake"

# Production modules allowed to call these (adapters implement the ports;
# bootstrap is the sole domain caller of both for Dev runs).
START_ALLOW = {
    "domain/run_bootstrap.py",
}
CREATE_USER_ALLOW = {
    "domain/run_bootstrap.py",
    "adapters/redis/messaging.py",   # implements the port
    "ports/messaging.py",            # Protocol stub body may be empty
}


def _py_files():
    for p in ROOT.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT)).replace("\\", "/")


def _calls_attr(tree: ast.AST, attr: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == attr:
                return True
    return False


def test_executor_start_only_from_run_bootstrap():
    offenders = []
    for path in _py_files():
        rel = _rel(path)
        if rel in START_ALLOW:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        if _calls_attr(tree, "start"):
            # Narrow: only flag `.start(` where the receiver name looks like
            # an executor (executor / self.executor / self.bootstrap.executor
            # is in bootstrap only). Broad attr=="start" false-positives on
            # httpx/asyncio — require the call is `something.start(` with
            # args matching the ExecutorPort shape is hard in AST; instead
            # require the attribute chain contains "executor".
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "start"):
                    continue
                # walk Attribute chain for a Name/Attribute "executor"
                cur = node.func.value
                names: list[str] = []
                while isinstance(cur, ast.Attribute):
                    names.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    names.append(cur.id)
                if "executor" in names:
                    offenders.append(rel)
                    break
    assert not offenders, (
        "executor.start must only be called from RunBootstrap.launch "
        f"(clear-runs dispatch_lock chokepoint): {offenders}"
    )


def test_create_run_user_only_from_bootstrap_or_adapter():
    offenders = []
    for path in _py_files():
        rel = _rel(path)
        if rel in CREATE_USER_ALLOW:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "create_run_user"):
                offenders.append(rel)
                break
    assert not offenders, (
        "create_run_user for Devs must go through RunBootstrap.launch "
        f"(or the redis adapter implementation): {offenders}"
    )


def test_prod_wires_a_real_workspace_store():
    """AUD-016: RunManager/RunBootstrap default to NullWorkspaceStore (a silent
    no-op) so the existing suite stays untouched — therefore prod composition
    MUST inject the real WorkspaceStore, or every dispatch would run onto an
    unmanaged workspace with no pre-create, cleanup, or fail-closed gate.
    Guard the wiring so dropping it fails CI rather than silently degrading.
    ADR-0028: the composition root lives in api/services.py now.

    AST, not substring (2026-08-12 audit test-gap): `"WorkspaceStore(" in src`
    is ALSO satisfied by `NullWorkspaceStore(` — the exact silent-no-op
    regression the guard exists to catch — and by `workspaces=workspaces`
    even if the object bound there is null. This checks a real call to the
    class whose name is EXACTLY WorkspaceStore, passed as workspaces=."""
    tree = ast.parse((ROOT / "api" / "services.py").read_text())
    assigned_from_real = False
    for n in ast.walk(tree):
        if not isinstance(n, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "workspaces"
                   for t in n.targets):
            continue
        if (isinstance(n.value, ast.Call)
                and isinstance(n.value.func, ast.Name)
                and n.value.func.id == "WorkspaceStore"):
            assigned_from_real = True
    assert assigned_from_real, (
        "prod must bind workspaces = WorkspaceStore(...) — a decoy "
        "WorkspaceStore = NullWorkspaceStore then WorkspaceStore(...) "
        "does not count unless the name on the Assign is the real class")
    wired = any(
        isinstance(n, ast.Call)
        and any(kw.arg == "workspaces"
                and isinstance(kw.value, ast.Name)
                and kw.value.id == "workspaces"
                for kw in n.keywords)
        for n in ast.walk(tree))
    assert wired, "RunManager must be wired workspaces=<the real store>"
    # the legacy substring assertions kept as a cheap belt-and-braces
    src = (ROOT / "api" / "services.py").read_text()
    assert "WorkspaceStore(" in src, "prod must construct a real WorkspaceStore"
    assert "workspaces=workspaces" in src, \
        "RunManager must be wired with the real workspace store"
