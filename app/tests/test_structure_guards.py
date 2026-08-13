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


# ── ADR-0034: checkpoint step keys resolve to the registry ───────────────────

DOMAIN = Path(__file__).parents[1] / "devcake" / "domain"

# The two sanctioned domain→adapters seams (2026-08-12 audit, docs audit §3):
# git is the single subprocess seam (ADR-0024 §"single subprocess seam");
# adapters.http's pool cleanup rides the hot-reload path (F16 — documented
# by this allowlist entry and the module's own comment).
DOMAIN_ADAPTER_IMPORT_ALLOWLIST = {
    ("repo_mirror.py", "adapters.git"),
    ("forge_runtime.py", "adapters.http"),
}

# keyed by path relative to domain/ so a new nested file cannot inherit
# a basename allowlist entry
DOMAIN_ADAPTER_IMPORT_ALLOWLIST_REL = {
    ("repo_mirror.py", "adapters.git"),
    ("forge_runtime.py", "adapters.http"),
}


def _steps_module():
    from devcake.domain.orchestrator import steps
    return steps


def _step_key_expr_ok(node, local_assigns, steps) -> bool:
    """A registered key expression: a literal in EXACT_KEYS, `steps.<CONST>`
    naming a registered constant, a family constructor call, or a local Name
    assigned from one of those."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value in steps.EXACT_KEYS
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "steps"):
        val = getattr(steps, node.attr, None)
        return isinstance(val, str) and val in steps.EXACT_KEYS
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "steps"):
        return node.func.attr in steps.FAMILY_CONSTRUCTORS
    if isinstance(node, ast.Name):
        assigned = local_assigns.get(node.id)
        return assigned is not None and _step_key_expr_ok(assigned, {}, steps)
    return False


def _checkpoint_key_node(node) -> ast.AST | None:
    """Key expression of a `_checkpoint(...)` call (Name or Attribute)."""
    f = node.func
    is_ckpt = ((isinstance(f, ast.Name) and f.id == "_checkpoint")
               or (isinstance(f, ast.Attribute) and f.attr == "_checkpoint"))
    if not is_ckpt:
        return None
    for kw in node.keywords:
        if kw.arg == "key":
            return kw.value
    # Name: _checkpoint(mgr, run, key, fn) → args[2]
    # Attribute 3-arg: mgr._checkpoint(run, key, fn) → args[1]
    # Attribute 4-arg: finalize._checkpoint(mgr, run, key, fn) → args[2]
    if isinstance(f, ast.Name):
        return node.args[2] if len(node.args) >= 3 else None
    if len(node.args) == 3:
        return node.args[1]
    if len(node.args) >= 4:
        return node.args[2]
    return None


def scan_step_key_sites(source: str, steps) -> list[str]:
    """Offending `_checkpoint(...)` / `finalized_steps.append/extend` keys.

    Anti-accident: literals, `steps.*`, family constructors, per-function
    local assigns. The mechanism body of a function *named* `_checkpoint`
    is exempt (finalize._checkpoint's own `append(key)`).
    """
    tree = ast.parse(source)
    mechanism = {id(n) for fn in ast.walk(tree)
                 if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and fn.name == "_checkpoint"
                 for n in ast.walk(fn)}
    offenders = []

    def _locals(fn) -> dict[str, ast.AST]:
        local_assigns: dict[str, ast.AST] = {}
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                local_assigns[node.targets[0].id] = node.value
        return local_assigns

    functions = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not functions:
        functions = [tree]
    module_assigns = _locals(tree)

    for fn in functions:
        local_assigns = {**module_assigns, **_locals(fn)}
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or id(node) in mechanism:
                continue
            f = node.func
            keys: list[ast.AST] = []
            ck = _checkpoint_key_node(node)
            if ck is not None:
                keys.append(ck)
            elif (isinstance(f, ast.Attribute) and f.attr == "append"
                    and isinstance(f.value, ast.Attribute)
                    and f.value.attr == "finalized_steps"):
                if node.args:
                    keys.append(node.args[0])
            elif (isinstance(f, ast.Attribute) and f.attr == "extend"
                    and isinstance(f.value, ast.Attribute)
                    and f.value.attr == "finalized_steps"):
                arg0 = node.args[0] if node.args else None
                if isinstance(arg0, ast.List):
                    keys.extend(arg0.elts)
                elif arg0 is not None:
                    keys.append(arg0)
            for key in keys:
                if not _step_key_expr_ok(key, local_assigns, steps):
                    offenders.append(f"line {node.lineno}: {ast.unparse(key)}")
    return offenders


def test_every_checkpoint_key_resolves_to_the_registry():
    """ADR-0034 / audit F13: `run.finalized_steps` strings are the
    redelivery control flow — every producer site must use a registered
    constant or family constructor from orchestrator/steps.py, never a bare
    literal the two derived tables (swap-marker stage, past-gate) can't see.
    Remediation: register the key in steps.py (declaring stage_after /
    past_freshness_gate) or use its family constructor."""
    steps = _steps_module()
    offenders = []
    for p in sorted(DOMAIN.rglob("*.py")):
        if p.name == "steps.py" or "__pycache__" in p.parts:
            continue
        offenders += [f"{p.relative_to(DOMAIN)}: {o}"
                      for o in scan_step_key_sites(p.read_text(), steps)]
    assert not offenders, (
        "unregistered checkpoint keys (register in orchestrator/steps.py or "
        "use its family constructor): " + "; ".join(offenders))


def test_every_registry_key_is_used():
    """Reverse ratchet: the registry accumulates no dead rows — every exact
    key's constant and every family constructor is referenced somewhere in
    domain/ (as `steps.<NAME>`)."""
    steps = _steps_module()
    used: set[str] = set()
    for p in sorted(DOMAIN.rglob("*.py")):
        if p.name == "steps.py" or "__pycache__" in p.parts:
            continue
        for node in ast.walk(ast.parse(p.read_text())):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "steps"):
                used.add(node.attr)
    const_by_key = {getattr(steps, name): name for name in dir(steps)
                    if isinstance(getattr(steps, name), str)
                    and not name.startswith("_")}
    dead = sorted(const_by_key[s.key] for s in steps.REGISTRY
                  if not s.dynamic and const_by_key.get(s.key) not in used)
    dead += sorted(f for f in steps.FAMILY_CONSTRUCTORS if f not in used)
    assert not dead, f"registry rows with no producer in domain/: {dead}"


def test_domain_never_imports_adapters():
    """The layering rule (docs/01: domain depends on port Protocols, never
    adapter packages), previously enforced by prose and review culture only
    (2026-08-12 docs audit). Two sanctioned seams are allowlisted above;
    a third needs its own documented ruling, not a quiet import."""
    offenders = []
    for p in sorted(DOMAIN.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        for node in ast.walk(ast.parse(p.read_text())):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.module:
                if "adapters" in node.module.split("."):
                    mods.append(node.module)
            elif isinstance(node, ast.Import):
                mods += [a.name for a in node.names
                         if "adapters" in a.name.split(".")]
            elif (isinstance(node, ast.ImportFrom) and not node.module
                    and any(a.name == "adapters" or a.name.startswith("adapters.")
                            for a in node.names)):
                mods.append("adapters")
            for m in mods:
                short = m[m.index("adapters"):]
                rel = str(p.relative_to(DOMAIN))
                if (rel, short) not in DOMAIN_ADAPTER_IMPORT_ALLOWLIST_REL:
                    offenders.append(f"{rel}: imports {m}")
    assert not offenders, (
        "domain must not import adapter packages (ADR-0034 layering ratchet; "
        "sanctioned seams live in the allowlist): " + "; ".join(offenders))


# ADR-0033, founder ruling 2026-08-13 (2a): DEVCAKE-DISCOVERY is a pure
# sweep gate. model.py is allowlisted only for the *constant definition*
# (ALL_LABELS / ensure_labels). derive() lives in that file — a scheduling
# read there would pass the file-level grep — so derive() is AST-walked
# separately. steward.py is allowlisted for *removal* at finalize, never
# a scheduling read.
DISCOVERY_LABEL_ALLOWLIST_REL = {"model.py", "orchestrator/discovery.py",
                                 "orchestrator/steward.py"}


def test_derive_never_reads_discovery_label():
    """The behavioral pin is test_spa_contracts.test_discovery_label_is_
    derivation_inert. This AST walk is the ratchet: derive() cannot grow
    a LABEL_DISCOVERY / DEVCAKE-DISCOVERY read while model.py stays
    allowlisted for the constant."""
    import ast
    src = (DOMAIN / "model.py").read_text()
    tree = ast.parse(src)
    offenders = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "derive":
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and child.id == "LABEL_DISCOVERY":
                    offenders.append("LABEL_DISCOVERY")
                if isinstance(child, ast.Constant) and isinstance(child.value, str) \
                        and "DEVCAKE-DISCOVERY" in child.value:
                    offenders.append(child.value)
    assert not offenders, (
        "derive() must stay discovery-label-inert: " + ", ".join(offenders))


def test_discovery_label_never_joins_scheduling():
    offenders = []
    for p in sorted(DOMAIN.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(DOMAIN).as_posix()
        if rel in DISCOVERY_LABEL_ALLOWLIST_REL:
            continue
        src = p.read_text()
        if "LABEL_DISCOVERY" in src or "DEVCAKE-DISCOVERY" in src:
            offenders.append(rel)
    assert not offenders, (
        "DEVCAKE-DISCOVERY is a sweep gate only (ADR-0033 founder ruling): "
        "a new consumer needs its own documented ruling and an allowlist "
        "entry, never a quiet read: " + "; ".join(offenders))
