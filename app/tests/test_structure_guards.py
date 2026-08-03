"""Structure ratchet (ADR-0015): the god modules must not return.

C1 rule: ``domain/orchestrator/manager.py`` contains the class and nothing
executable after it — no module-level ``MissionManager.<attr> = ...`` bindings
(the old façade mechanism) and no module-level ``def`` below the class.

C6 rule: ``api/main.py`` is composition root + route forwards — every
``@app.<verb>`` route body is ≤ 4 statements (docstring excluded). Endpoint
behavior lives in ``api/`` service modules. The allowlist below is the
residual read-side surface at close-out; it may SHRINK, never grow — a new
endpoint that needs more than a forward gets a service module.
"""

import ast
from pathlib import Path

MANAGER = (Path(__file__).parents[1] / "devcake" / "domain" / "orchestrator"
           / "manager.py")
MAIN = Path(__file__).parents[1] / "devcake" / "api" / "main.py"

# Residual bodies allowed in main.py (C6 close-out; shrink opportunistically):
# the CI fixture, the mapper/OAuth trio, and the small read-side runs/log/
# clear endpoints. Everything else forwards to a service module.
ROUTE_BODY_ALLOWLIST = {
    "dispatch_hello", "run_mapper", "oauth_start", "oauth_status",
    "clear_runs", "get_run_log", "stream_run_log",
}


def test_manager_has_no_post_class_bindings():
    tree = ast.parse(MANAGER.read_text())
    class_idx = next(i for i, n in enumerate(tree.body)
                     if isinstance(n, ast.ClassDef) and n.name == "MissionManager")
    offenders = []
    for node in tree.body[class_idx + 1:]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            offenders.append(f"module-level def {node.name} after the class")
        for tgt in getattr(node, "targets", []):
            if (isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name)
                    and tgt.value.id == "MissionManager"):
                offenders.append(f"binding MissionManager.{tgt.attr}")
    assert not offenders, (
        "manager.py grew post-class bindings — ADR-0015 forbids resurrecting "
        "the façade: " + "; ".join(offenders))


def _is_route(node) -> bool:
    for dec in node.decorator_list:
        call = dec if isinstance(dec, ast.Call) else None
        target = call.func if call else dec
        if (isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "app"):
            return True
    return False


def test_lifespan_never_awaits_the_forge_sweep():
    """Incident 2026-08-01: `await refresh_forge_health()` inside lifespan
    held the listen socket for O(N repos) of probe I/O and failed the compose
    healthcheck. The sweep belongs to the poll task (before its first cycle);
    this guard keeps the boot regression from coming back silently."""
    tree = ast.parse(MAIN.read_text())
    lifespan = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "lifespan")
    offenders = [
        node.lineno for node in ast.walk(lifespan)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "refresh_forge_health"
    ]
    assert not offenders, (
        "lifespan awaits refresh_forge_health again (lines "
        f"{offenders}) — boot must not block on the forge sweep")


def test_lifespan_never_awaits_mirror_warmup():
    """ADR-0024: a cold 27-repo warm-up clones for minutes — it belongs to a
    background task started by the poll loop; awaiting any RepoCache sync in
    lifespan would recreate the 2026-08-01 boot-blocking incident shape."""
    tree = ast.parse(MAIN.read_text())
    lifespan = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "lifespan")
    offenders = [
        node.lineno for node in ast.walk(lifespan)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr in ("warm_all", "ensure_fresh", "sync_one")
    ]
    assert not offenders, (
        f"lifespan awaits a RepoCache sync (lines {offenders}) — mirror "
        "warm-up must ride a background task, never boot")


def test_main_route_bodies_stay_thin():
    tree = ast.parse(MAIN.read_text())
    offenders = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_route(node) or node.name in ROUTE_BODY_ALLOWLIST:
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]                       # docstring doesn't count
        if len(body) > 4:
            offenders.append(f"{node.name} ({len(body)} statements)")
    assert not offenders, (
        "main.py route bodies grew past 4 statements — move the behavior to "
        "an api/ service module (ADR-0015 Decision 3): " + "; ".join(offenders))
