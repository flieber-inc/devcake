"""Model-backend degradation detector and the dispatch throttle (ADR-0018).

The brake is NOT a circuit breaker: it caps a sick dev type to one probe run
instead of blocking it, so the store-derived condition can clear itself.
"""

import ast
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from devcake.config import DevType
from devcake.domain import backend_health
from devcake.domain.run import Run
from devcake.domain.orchestrator import schedule

T0 = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
FAULT = backend_health.FAULT_CLASS


def run(n, *, mission="p1", dev_type="judgment", error_class=FAULT,
        state="failed", counted=True, mtype="EXECUTE"):
    return Run(run_id=f"T-{n}-1-{mtype}-AAAAAA", mission_key="T-1",
               mission_pmo_id=mission, mission_type=mtype, dev_type=dev_type,
               seq=1, state=state, error_class=error_class,
               attempt_counted=counted, error=f"{error_class}: boom",
               created_at=T0 + timedelta(minutes=n))


def ok(n, *, mission="p1", dev_type="judgment"):
    return run(n, mission=mission, dev_type=dev_type, error_class="",
               state="finished")


# ── backend_correlated: the ACCOUNTING predicate ─────────────────────────────

def test_trips_on_two_faults_across_two_missions():
    runs = [run(1, mission="p1"), run(2, mission="p2")]
    assert backend_correlated_reason(runs) is not None


# ── ADR-0026: opt-in widening to DEV_BAD_OUTPUT ──────────────────────────────

def test_bad_output_is_invisible_to_the_brake_by_default():
    """ADR-0018's design stands unless the operator opts in: exit-11 evidence
    never correlates, never throttles (the 2026-07-24 cascade behavior)."""
    runs = [run(1, mission="p1", error_class="DEV_BAD_OUTPUT"),
            run(2, mission="p2", error_class="DEV_BAD_OUTPUT")]
    assert backend_health.backend_correlated(runs, "judgment") is None
    assert backend_health.backend_degraded(runs, "judgment") is None
    assert backend_health.fault_classes(False) == frozenset(
        {backend_health.FAULT_CLASS})


def test_bad_output_correlates_when_opted_in():
    """`brake_on_bad_output: true` makes a fleet-wide bad-output cascade read
    as ONE backend event — including MIXED evidence (some containers exit 15,
    others 11), which is exactly the shared-backend failure shape."""
    classes = backend_health.fault_classes(True)
    pure = [run(1, mission="p1", error_class="DEV_BAD_OUTPUT"),
            run(2, mission="p2", error_class="DEV_BAD_OUTPUT")]
    assert backend_health.backend_correlated(pure, "judgment",
                                             classes=classes) is not None
    mixed = [run(1, mission="p1", error_class="DEV_BAD_OUTPUT"),
             run(2, mission="p2", error_class=FAULT)]
    assert backend_health.backend_correlated(mixed, "judgment",
                                             classes=classes) is not None
    assert backend_health.backend_degraded(mixed, "judgment",
                                           classes=classes) is not None


def test_bad_output_solo_streak_throttles_when_opted_in():
    classes = backend_health.fault_classes(True)
    runs = [run(1, error_class="DEV_BAD_OUTPUT"),
            run(2, error_class="DEV_BAD_OUTPUT"),
            run(3, error_class="DEV_BAD_OUTPUT")]
    assert backend_health.backend_correlated(runs, "judgment",
                                             classes=classes) is None
    assert backend_health.backend_degraded(runs, "judgment",
                                           classes=classes) is not None


def test_bad_output_excusals_are_class_scoped():
    """The DEV_BAD_OUTPUT excusal budget is its own ledger: harness-fault
    excusals must not consume it and vice versa (same per-step bound)."""
    probe = run(9, error_class="DEV_BAD_OUTPUT")
    spent_harness = [run(n, error_class=FAULT, counted=False)
                     for n in (1, 2, 3)]
    assert backend_health.excusals_left(
        spent_harness + [probe], probe, error_class="DEV_BAD_OUTPUT")
    spent_bad = [run(n, error_class="DEV_BAD_OUTPUT", counted=False)
                 for n in (1, 2, 3)]
    assert not backend_health.excusals_left(
        spent_bad + [probe], probe, error_class="DEV_BAD_OUTPUT")


def backend_correlated_reason(runs, dev_type="judgment"):
    return backend_health.backend_correlated(runs, dev_type)


def test_a_single_mission_looping_never_correlates():
    """Anti-livelock: one mission failing repeatedly is the mission's problem,
    so its failures keep counting and it still reaches DEVCAKE-FAILED."""
    runs = [run(1), run(2), run(3)]          # all mission p1
    assert backend_correlated_reason(runs) is None


def test_a_single_mission_looping_still_throttles():
    """…but it DOES throttle. A deployment with one active mission per dev type
    can never satisfy MIN_DISTINCT_MISSIONS, so correlation alone would protect
    it not at all."""
    runs = [run(1), run(2), run(3)]
    assert backend_health.backend_degraded(runs, "judgment") is not None


def test_solo_streak_survives_an_interleaved_steward_run():
    """The solo arm selects its OWN evidence (the 3 most recent MISSION-BEARING
    terminal runs). Filtering mission-bearing runs out of the already-truncated
    3-window instead let ONE PMO-less run — a Relations Steward sharing the dev
    type — cap the list at 2 forever, making the arm dead in exactly the
    single-mission deployment it exists for."""
    runs = [run(1), run(2), run(3, mission="", mtype="STEWARD"), run(4)]
    assert backend_health.backend_degraded(runs, "judgment") is not None
    assert backend_health.backend_correlated(runs, "judgment") is None


def test_steward_only_faults_never_throttle_the_dev_type():
    """The mission-bearing filter still holds: the steward has its own
    degraded() back-off and must not throttle a dev type's real missions."""
    runs = [run(i, mission="", mtype="STEWARD") for i in (1, 2, 3)]
    assert backend_health.backend_degraded(runs, "judgment") is None


def test_backend_degraded_is_a_superset_of_backend_correlated():
    runs = [run(1, mission="p1"), run(2, mission="p2")]
    assert backend_health.backend_correlated(runs, "judgment") is not None
    assert backend_health.backend_degraded(runs, "judgment") is not None


def test_other_dev_types_are_unaffected():
    runs = [run(1, mission="p1"), run(2, mission="p2")]
    assert backend_health.backend_degraded(runs, "implementer") is None


def test_steward_runs_never_satisfy_the_distinct_mission_rule():
    """STEWARD runs carry mission_pmo_id == "" and the Relations Steward's dev
    type is operator-configurable — it may legitimately BE `judgment`. Without
    the truthy filter, one STEWARD fault plus one mission fault would read as
    two distinct missions."""
    runs = [run(1, mission="", mtype="STEWARD"), run(2, mission="p2")]
    assert backend_health.backend_correlated(runs, "judgment") is None


def test_turn_budget_is_not_correlation_evidence():
    runs = [run(1, mission="p1", error_class="DEV_TURN_BUDGET"),
            run(2, mission="p2", error_class="DEV_TURN_BUDGET")]
    assert backend_health.backend_correlated(runs, "judgment") is None


def test_two_greens_clear_it_and_one_does_not():
    """Successes are included in the window ON PURPOSE — evicting fault
    evidence is the entire clearing mechanism."""
    faults = [run(1, mission="p1"), run(2, mission="p2")]
    assert backend_health.backend_correlated(faults + [ok(3)], "judgment") is not None
    assert backend_health.backend_correlated(faults + [ok(3), ok(4)], "judgment") is None


def test_naive_created_at_does_not_crash_the_detector():
    r = run(1)
    r.created_at = datetime(2026, 7, 24, 12, 0)      # tz-naive
    assert backend_health.backend_degraded([r], "judgment") is None


# ── excusals: the escape hatch ───────────────────────────────────────────────

def test_excusals_run_out_per_step():
    used = [run(i, counted=False) for i in range(1, backend_health.MAX_EXCUSALS_PER_STEP + 1)]
    assert backend_health.excusals_left([], run(9)) is True
    assert backend_health.excusals_left(used, run(9)) is False


def test_a_finished_run_restores_the_excusal_allowance():
    used = [run(i, counted=False) for i in range(1, 5)]
    assert backend_health.excusals_left(used + [ok(9)], run(10)) is True


def test_excusals_are_scoped_to_the_step():
    used = [run(i, counted=False, mtype="ONBOARD") for i in range(1, 5)]
    assert backend_health.excusals_left(used, run(9, mtype="EXECUTE")) is True


# ── refresh_degraded: the shared map ─────────────────────────────────────────

def test_refresh_mutates_in_place_and_reports_only_new_entries():
    """The dict identity is shared with every MissionManager — rebinding it
    would silently disconnect every reader."""
    shared = {}
    runs = [run(1, mission="p1"), run(2, mission="p2")]
    entered = backend_health.refresh_degraded(runs, shared, {"judgment"})
    assert entered == ["judgment"] and "judgment" in shared
    again = backend_health.refresh_degraded(runs, shared, {"judgment"})
    assert again == [], "an already-degraded dev type must not re-fire its span"
    healthy = [ok(5), ok(6), ok(7)]
    backend_health.refresh_degraded(healthy, shared, {"judgment"})
    assert shared == {}


def test_refresh_ignores_runs_with_no_dev_type():
    shared = {}
    r = run(1)
    r.dev_type = ""
    backend_health.refresh_degraded([r], shared, {"judgment"})
    assert shared == {}


def test_refresh_drops_dev_types_absent_from_the_live_registry():
    """A renamed or deleted dev type can never produce the two greens that clear
    degradation, so a run-derived-only key would sit degraded FOREVER with no
    control to clear it — while the renamed type inherits no evidence and
    silently dispatches at full concurrency mid-outage."""
    shared = {}
    runs = [run(1, mission="p1"), run(2, mission="p2")]
    assert backend_health.refresh_degraded(
        runs, shared, {"judgment"}) == ["judgment"]
    backend_health.refresh_degraded(runs, shared, {"judgment-v2"})
    assert shared == {}, "a dev type absent from the registry must be dropped"
    backend_health.refresh_degraded(runs, shared, {"judgment", "implementer"})
    assert "judgment" in shared, "a live dev type must survive the refresh"
    backend_health.refresh_degraded(runs, shared, set())
    assert shared == {}, "no managers ⇒ no live dev types ⇒ empty map"


def test_poll_builds_the_registry_from_the_managers_it_holds():
    """ADR-0015: poll may NOT import the composition root to reach the dev-type
    registry — the managers are already injected and each carries `dev_types`."""
    from devcake.api import poll
    src = Path(inspect.getfile(poll)).read_text()
    assert "dev_types" in src, \
        "poll no longer threads the live dev-type registry into refresh_degraded"
    imported: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any(name.split(".")[-1] == "main" for name in imported), \
        "poll must not import the composition root (ADR-0015)"


# ── the dispatch throttle ────────────────────────────────────────────────────

def test_degraded_caps_concurrency_to_one_probe():
    dev_type = DevType(name="judgment", harness_template="claude-code",
                       max_concurrency=3)
    active = [run(1, state="running")]
    degraded = {"judgment": "backend sick"}
    cap = (backend_health.DEGRADED_CONCURRENCY if dev_type.name in degraded
           else dev_type.max_concurrency)
    assert cap == 1
    assert sum(1 for r in active if r.dev_type == dev_type.name) >= cap, \
        "one in-flight probe must block a second dispatch"
    assert backend_health.DEGRADED_CONCURRENCY == 1, "the probe is the half-open"


def test_the_cap_is_a_throttle_not_a_block():
    """With nothing in flight, a degraded dev type still dispatches ONE run —
    that probe is what lets the condition clear itself."""
    degraded = {"judgment": "backend sick"}
    active = []
    cap = backend_health.DEGRADED_CONCURRENCY if "judgment" in degraded else 3
    assert sum(1 for r in active if r.dev_type == "judgment") < cap


def test_schedule_reads_the_cap_from_backend_degraded():
    """Guard the wiring: schedule() must consult mgr.backend_degraded, not a
    breaker map."""
    src = inspect.getsource(schedule.schedule)
    assert "backend_degraded" in src and "DEGRADED_CONCURRENCY" in src


def test_degradation_is_not_a_circuit_breaker():
    """It must never latch the dev-type or per-repo breaker maps: those are
    documented as cleared only by a credential write, and this self-heals."""
    src = inspect.getsource(backend_health)
    assert "_trip_breaker" not in src and "forges.latch" not in src


# ── the poll placement trap ──────────────────────────────────────────────────

def _run_cycle_body():
    from devcake.api import poll
    tree = ast.parse(Path(inspect.getfile(poll)).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_cycle":
            return node
    raise AssertionError("run_cycle not found")


def test_refresh_is_unconditional_and_precedes_dispatch():
    """BOTH halves of this fail silently if broken.

    One indent deeper the refresh sits inside `if self.forge_runtime.breakers:`
    and never runs in the common case, leaving the map empty and the brake
    permanently disarmed. After the instance loop, the throttle lags a whole
    poll interval — long enough for the fleet to be re-dispatched at full
    concurrency into a broken backend.
    """
    body = _run_cycle_body()
    refresh_line = loop_line = None
    for node in ast.walk(body):
        if isinstance(node, ast.Attribute) and node.attr == "refresh_degraded":
            refresh_line = node.lineno
        if isinstance(node, ast.AsyncFor) or isinstance(node, ast.For):
            call = ast.dump(node.iter)
            if "managers_in_config_order" in call:
                loop_line = node.lineno
    assert refresh_line, "run_cycle no longer refreshes the degraded map"
    assert loop_line, "the instance loop moved — re-pin this guard"
    assert refresh_line < loop_line, \
        "refresh_degraded must run BEFORE the instance loop that dispatches"

    # and it must not be nested under the forge-breaker conditional
    for node in ast.walk(body):
        if isinstance(node, ast.If) and "forge_runtime" in ast.dump(node.test):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute) and inner.attr == "refresh_degraded":
                    pytest.fail("refresh_degraded is nested inside the "
                                "forge-breaker `if` — it would be dead code "
                                "whenever no forge breaker is latched")
