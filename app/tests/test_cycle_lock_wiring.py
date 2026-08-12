"""The secret/config mutation ROUTES must thread cycle_lock (2026-08-12 audit
test-gap): test_secret_reload_lock proves the SERVICE blocks when handed a
held lock, but nothing guarded that the routes actually PASS s.poll_rt.lock —
deleting `cycle_lock=...` from a route (the L-1 regression class) left every
test green. This is the auth-surface guard's shape (test_api_surface) applied
to the lock wiring: an AST scan over main.py's route bodies."""

import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "devcake" / "api" / "main.py"

# Routes whose handler MUST forward the poll-cycle lock into its service call
# (config/secret world-swaps — a mid-cycle mutation swaps the adapter graph
# under a suspended poll segment; ADR-0015 L-1/L-2). Keyed by the service
# function the route forwards to.
LOCK_REQUIRED_CALLS = {
    "apply_config_patch",
    "put_secret",
    "delete_secret",
    "clear_secrets",
}

# apply_profile holds the lock via `async with s.poll_rt.lock` (different
# shape — not a cycle_lock= kwarg). Harness PUT/DELETE and credential
# upload are live-read at dispatch and are listed here so a reviewer
# does not assume they were forgotten.
LOCK_VIA_WITH = {"apply_profile"}
LIVE_READ_UNLOCKED = {
    "put_harness_secret", "delete_harness_secret", "upload_credentials",
}


def _service_calls(tree):
    """Every Call whose func is `<module>.<name>` or a bare `<name>`, yielded
    as (name, node) — the route bodies forward to service functions this way."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute):
            yield f.attr, node
        elif isinstance(f, ast.Name):
            yield f.id, node


def test_mutation_routes_thread_the_poll_cycle_lock():
    tree = ast.parse(MAIN.read_text())
    seen = {name: False for name in LOCK_REQUIRED_CALLS}
    offenders = []
    for name, call in _service_calls(tree):
        if name not in LOCK_REQUIRED_CALLS:
            continue
        seen[name] = True
        has_lock = any(kw.arg == "cycle_lock" for kw in call.keywords)
        # the value must be the real poll lock, not None/a placeholder
        lock_kw = next((kw for kw in call.keywords if kw.arg == "cycle_lock"),
                       None)
        unparsed = ast.unparse(lock_kw.value) if lock_kw is not None else ""
        good = has_lock and "poll_rt.lock" in unparsed
        if not good:
            offenders.append(name)
    missing = [n for n, hit in seen.items() if not hit]
    assert not missing, (
        f"expected these lock-required routes in main.py but did not find "
        f"their service calls (renamed? move the name into this guard): "
        f"{missing}")
    assert not offenders, (
        f"these mutation routes do not forward cycle_lock=<poll_rt.lock> — "
        f"a mid-cycle secret/config write would swap the adapter graph under "
        f"a suspended poll segment (L-1/L-2): {offenders}")
    src = MAIN.read_text()
    assert "poll_rt.lock" in src and "apply_profile" in src, (
        "apply_profile must hold poll_rt.lock (async with, not cycle_lock=)")
