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
# the CI fixture, the steward/OAuth trio, and the small read-side runs/log/
# clear endpoints. Everything else forwards to a service module.
ROUTE_BODY_ALLOWLIST = {
    "dispatch_hello", "run_steward", "oauth_start", "oauth_status",
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
    this guard keeps the boot regression from coming back silently.

    ADR-0028: matches BOTH call shapes — the old bare name and the factored
    `s.refresh_forge_health()` / `forge_runtime.refresh_all()` attribute
    calls the Services move introduced (a Name-only guard would go blind the
    day the sweep becomes a method)."""
    tree = ast.parse(MAIN.read_text())
    lifespan = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "lifespan")
    sweep_names = {"refresh_forge_health", "refresh_all"}
    offenders = [
        node.lineno for node in ast.walk(lifespan)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and ((isinstance(node.value.func, ast.Name)
              and node.value.func.id in sweep_names)
             or (isinstance(node.value.func, ast.Attribute)
                 and node.value.func.attr in sweep_names))
    ]
    assert not offenders, (
        "lifespan awaits the forge sweep again (lines "
        f"{offenders}) — boot must not block on it")


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


# ── ADR-0028: the composition root is a factory, not an import side effect ───

def test_importing_main_is_side_effect_free(tmp_path):
    """Importing api.main must build NOTHING: no /data reads-that-write, no
    adapter construction, no config.yaml write. A subprocess is required —
    the in-process module cache would hide a second import's effects. The
    env is hostile on purpose: ADMIN_* unset, a fresh empty DEVCAKE_DATA_DIR
    — a side-effecting import either crashes or leaves files, and both fail
    here."""
    import os
    import subprocess
    import sys

    data = tmp_path / "data"
    data.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("ADMIN_USER", "ADMIN_PASSWORD")}
    env["DEVCAKE_DATA_DIR"] = str(data)
    proc = subprocess.run(
        [sys.executable, "-c", "import devcake.api.main"],
        capture_output=True, text=True, env=env,
        cwd=str(Path(__file__).parents[1]))
    assert proc.returncode == 0, (
        f"import devcake.api.main failed in a bare env:\n{proc.stderr[-2000:]}")
    created = sorted(str(p.relative_to(data)) for p in data.rglob("*"))
    assert not created, (
        f"importing api.main wrote into DEVCAKE_DATA_DIR: {created} — "
        "construction belongs in build_services() (ADR-0028)")


def test_main_module_level_is_wiring_only():
    """The filesystem probe above cannot see a re-added `Messaging(...)` at
    module scope (client construction writes nothing). This AST allowlist
    can: every top-level call in api/main.py must be pure wiring."""
    ALLOWED = {"logging.basicConfig", "logging.getLogger", "trace.get_tracer",
               "FastAPI", "app.middleware", "FastAPIInstrumentor.instrument_app"}

    def _name(func) -> str:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            base = _name(func.value)
            return f"{base}.{func.attr}" if base else func.attr
        if isinstance(func, ast.Call):        # app.middleware("http")(fn)
            return _name(func.func)
        return ""

    tree = ast.parse(MAIN.read_text())
    offenders = []
    for node in tree.body:
        # defs/classes run at CALL time; only bare module-level statements
        # execute on import
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Import, ast.ImportFrom)):
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and _name(call.func) not in ALLOWED:
                offenders.append(f"line {call.lineno}: {_name(call.func)}()")
    assert not offenders, (
        "api/main.py module scope grew non-wiring calls — move them into "
        "build_services() / lifespan (ADR-0028): " + "; ".join(offenders))
