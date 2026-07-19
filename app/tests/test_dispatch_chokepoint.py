"""Dispatch chokepoint tripwire (independent review residual G).

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
