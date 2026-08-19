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


def _attr_chain_names(node: ast.AST) -> list[str]:
    """Names along an Attribute/Name chain (outermost attr first)."""
    names: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        names.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        names.append(cur.id)
    return names


def _executor_aliases(tree: ast.AST) -> set[str]:
    """Names bound to an attribute chain that contains ``executor``.

    Catches ``ex = self.executor`` / ``ex = mgr.executor`` so a later
    ``ex.start(...)`` cannot bypass the chokepoint ratchet.
    """
    aliases: set[str] = {"executor"}
    changed = True
    # Fixed-point: alias-of-alias (ex = self.executor; boot = ex).
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            names = _attr_chain_names(node.value)
            if isinstance(node.value, ast.Name):
                names = [node.value.id]
            if any(n in aliases or n == "executor" for n in names):
                if target.id not in aliases:
                    aliases.add(target.id)
                    changed = True
    return aliases


def module_calls_executor_start(tree: ast.AST) -> bool:
    """True when the module AST calls ``.start`` on an executor (or alias)."""
    aliases = _executor_aliases(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "start"):
            continue
        names = _attr_chain_names(node.func.value)
        if any(n in aliases for n in names):
            return True
        # Direct ``something.executor.start(...)`` — chain contains executor.
        if "executor" in names:
            return True
    return False


def test_executor_start_alias_bypass_is_detected():
    """An aliased executor.start outside the allowlist must fail CI — the
    literal-name-only scan let ``ex = self.executor; ex.start(...)`` through."""
    aliased = ast.parse(
        "async def launch(mgr):\n"
        "    ex = mgr.executor\n"
        "    await ex.start(run_id='r', params={})\n"
    )
    assert module_calls_executor_start(aliased)
    chained = ast.parse(
        "async def launch(self):\n"
        "    await self.bootstrap.executor.start(run_id='r', params={})\n"
    )
    assert module_calls_executor_start(chained)
    # httpx/asyncio .start must stay quiet (no executor in the chain).
    noise = ast.parse(
        "async def go(client):\n"
        "    await client.start()\n"
        "    await asyncio.TaskGroup().start(fn)\n"
    )
    assert not module_calls_executor_start(noise)


def test_executor_start_only_from_run_bootstrap():
    offenders = []
    for path in _py_files():
        rel = _rel(path)
        if rel in START_ALLOW:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        if module_calls_executor_start(tree):
            offenders.append(rel)
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
