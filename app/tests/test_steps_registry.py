"""steps.py registry (ADR-0034; audit F13): byte-value canaries (record
compatibility for in-flight finalizing runs is non-negotiable), derived-view
snapshots against the exact pre-registry literal tables, and the guard's own
self-test (test the test)."""

from devcake.domain.orchestrator import steps
from devcake.domain.orchestrator.freshness import _PAST_GATE_STEPS
from devcake.domain.orchestrator.markers import _SWAP_MARKER_STAGE

from test_structure_guards import scan_step_key_sites


def test_swap_marker_stage_matches_the_pre_registry_literal_table():
    """Byte-exact snapshot of the table markers.py carried by hand before
    the registry — the derived view must reproduce it verbatim."""
    assert _SWAP_MARKER_STAGE == {
        "transition:planned:labels": "DEVCAKE-EXECUTE",
        "transition:executed:labels": "DEVCAKE-REVIEW",
        "transition:plan_needed_attach:labels": "DEVCAKE-EXECUTE",
        "transition:plan_needed": "DEVCAKE-PLAN",
        "review:reject:labels": "DEVCAKE-EXECUTE",
        "review:conflict_routed": "DEVCAKE-EXECUTE",
        "review:done": None,
        "review:merge_failed": None,
        "review:merge_deferred": None,
        "review:awaiting_merge": None,
        "review:merge_settle": None,
    }


def test_past_gate_steps_matches_the_pre_registry_literal_tuple():
    assert set(_PAST_GATE_STEPS) == {
        "review:done", "review:merge", "review:merge_failed",
        "review:merge_deferred", "review:conflict_routed",
        "review:awaiting_merge", "review:merge_settle",
    }


def test_constant_byte_values_are_frozen():
    """Record compatibility: a renamed CONSTANT is a refactor; a changed
    VALUE strands every in-flight finalizing run at upgrade time."""
    assert steps.REVIEW_DONE == "review:done"
    assert steps.REVIEW_MERGE == "review:merge"
    assert steps.DELIVER_ZIP == "deliver:zip"
    assert steps.TRANSCRIPT == "transcript"
    assert steps.REPLY == "reply"
    assert steps.TOKEN_REPORT == "token_report"
    assert steps.TRANSITION == "transition"
    assert steps.TRANSITION_HUMAN_NEEDED == "transition:human_needed"
    assert steps.ACL_USER_DELETED == "acl_user_deleted"
    assert steps.DECOMP_CHILD(3) == "decomp:child:3"
    assert steps.DECOMP_REL(2, 5) == "decomp:rel:2->5"
    assert steps.DECOMP_INHERIT_IN("b1", 4) == "decomp:inherit:in:b1->4"
    assert steps.DECOMP_INHERIT_OUT(4, "d9") == "decomp:inherit:out:4->d9"


def test_registry_has_no_duplicate_keys():
    keys = [s.key for s in steps.REGISTRY]
    assert len(keys) == len(set(keys))


def test_the_guard_catches_unregistered_keys():
    """Test the test: the AST guard must flag bare literals, unknown
    constants, and unresolvable names — and pass every registered shape."""
    bad = (
        "async def f(mgr, run):\n"
        "    await mgr._checkpoint(run, 'review:brand_new_step', fn)\n"
        "    run.finalized_steps.append('another:literal')\n"
        "    key = mystery()\n"
        "    await mgr._checkpoint(run, key, fn)\n"
    )
    offenders = scan_step_key_sites(bad, steps)
    assert len(offenders) == 3, offenders

    good = (
        "from . import steps\n"
        "async def f(mgr, run, i, j):\n"
        "    await mgr._checkpoint(run, steps.REVIEW_DONE, fn)\n"
        "    await mgr._checkpoint(run, 'review:merge', fn)\n"
        "    rel = steps.DECOMP_REL(j, i)\n"
        "    await mgr._checkpoint(run, rel, fn)\n"
        "    run.finalized_steps.append(steps.DECOMP_CHILD(i))\n"
        "async def _checkpoint(mgr, run, key, fn):\n"
        "    run.finalized_steps.append(key)   # the mechanism is exempt\n"
    )
    assert scan_step_key_sites(good, steps) == []
