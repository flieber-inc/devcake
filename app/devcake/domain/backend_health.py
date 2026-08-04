"""Model-backend degradation detector (ADR-0018).

Store-derived throttle/excuse for repeated `DEV_HARNESS_FAULT` on a Dev Type —
not a circuit breaker (no credential write to clear; docs/15 §4a).

* `backend_correlated` — ACCOUNTING (≥2 missions): may set `attempt_counted=False`.
* `backend_degraded` — THROTTLING (correlated **or** solo streak): cap concurrency
  to one probe. Solo must throttle without free retries, so the predicates stay
  separate.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import failure_taxonomy

WINDOW = 3                  # most recent TERMINAL runs of a dev type
MIN_DISTINCT_MISSIONS = 2   # correlation needs evidence from ≥2 missions
SOLO_THROTTLE_STREAK = 3    # consecutive faults on one mission ⇒ throttle only
DEGRADED_CONCURRENCY = 1    # the probe that makes the condition self-clearing
MAX_EXCUSALS_PER_STEP = 3   # escape hatch: bounds the livelock (see below)

FAULT_CLASS = failure_taxonomy.DEV_HARNESS_FAULT
BAD_OUTPUT_CLASS = failure_taxonomy.DEV_BAD_OUTPUT
DEFAULT_CLASSES = failure_taxonomy.BRAKE_ALWAYS_CLASSES


def fault_classes(brake_on_bad_output: bool) -> frozenset[str]:
    """Evidence classes the brake correlates on (ADR-0026), derived from the
    taxonomy table's `brake_evidence` column (ADR-0027).

    The set is shared by every arm — window faults, correlation, degradation —
    so a mixed cascade (some containers exit 15, others 11) reads as ONE
    backend event when the operator opts in. Off keeps the ADR-0018 design:
    exit 11 is invisible to the brake.
    """
    if brake_on_bad_output:
        return (failure_taxonomy.BRAKE_ALWAYS_CLASSES
                | failure_taxonomy.BRAKE_OPT_IN_CLASSES)
    return failure_taxonomy.BRAKE_ALWAYS_CLASSES

# Successes are INCLUDED on purpose: they are what evicts fault evidence from the
# window, which is the entire clearing mechanism ("two greens clear it"). The
# exclusive reading would leave a degraded dev type degraded forever.
TERMINAL_STATES = ("finished", "failed", "timed_out", "orphaned")


def _aware(ts):
    """Anchor timestamps come from several sources; a stray naive one must not
    crash the detector (mirrors dispatch._aware)."""
    if ts is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _window(runs, dev_type: str, limit: int = WINDOW, *,
            mission_bearing: bool = False) -> list:
    rows = [r for r in runs
            if getattr(r, "dev_type", None) == dev_type
            and getattr(r, "state", None) in TERMINAL_STATES
            and (not mission_bearing or getattr(r, "mission_pmo_id", ""))]
    rows.sort(key=lambda r: _aware(getattr(r, "created_at", None)), reverse=True)
    return rows[:limit]


def _faults(window: list, classes: frozenset[str] = DEFAULT_CLASSES) -> list:
    return [r for r in window if getattr(r, "error_class", "") in classes]


def backend_correlated(runs, dev_type: str,
                       classes: frozenset[str] = DEFAULT_CLASSES):
    """Reason string when the recent evidence implicates the BACKEND rather than
    any one mission, else None. Only this predicate may excuse an attempt.

    `mission_pmo_id` must be truthy to count toward the distinct-mission rule:
    MAPPER runs carry `""` (mapper.py never sets it) and the Relations Mapper's
    dev type is operator-configurable, so it may legitimately be `judgment`.
    Without the filter one MAPPER fault plus one mission fault would read as
    `{"", "X"}` — "two distinct missions" — and degrade a dev type on the
    strength of a run that belongs to no mission at all.
    """
    window = _window(runs, dev_type)
    faults = _faults(window, classes)
    missions = {r.mission_pmo_id for r in faults if getattr(r, "mission_pmo_id", "")}
    if len(faults) >= 2 and len(missions) >= MIN_DISTINCT_MISSIONS:
        newest = faults[0]
        return (f"{len(faults)} of the last {len(window)} {dev_type} runs failed with "
                f"{'/'.join(sorted(classes))} across {len(missions)} missions "
                f"(latest: {newest.error or 'no detail'})")
    return None


def backend_degraded(runs, dev_type: str,
                     classes: frozenset[str] = DEFAULT_CLASSES):
    """Reason string when dispatch for this dev type should be throttled to a
    single probe run, else None. Superset of `backend_correlated`.

    The solo arm exists because a deployment with one active mission per dev
    type can never satisfy MIN_DISTINCT_MISSIONS, so correlation alone would
    protect it not at all. Those failures still COUNT (backend_correlated stays
    None), so the step still reaches DEVCAKE-FAILED — throttling without
    unbounded retry.

    It selects its OWN evidence — the SOLO_THROTTLE_STREAK most recent
    MISSION-BEARING terminal runs of the dev type, independent of the shared
    window above. Filtering mission-bearing runs OUT OF that already-truncated
    3-window instead made the arm dead in exactly the deployment it exists for:
    one PMO-less run (a Relations Mapper sharing the dev type) capped the list
    at 2 forever, so the streak could never be reached.
    """
    correlated = backend_correlated(runs, dev_type, classes)
    if correlated:
        return correlated
    # mission-bearing only: three MAPPER faults (mission_pmo_id == "") must not
    # throttle the real missions of a dev type that is otherwise healthy — the
    # mapper has its own degraded() back-off and is not gated by this map.
    solo = _window(runs, dev_type, SOLO_THROTTLE_STREAK, mission_bearing=True)
    # ONE mission: multi-mission evidence belongs to `backend_correlated` above,
    # which also excuses — this arm stays strictly the solitary fallback.
    missions = {r.mission_pmo_id for r in solo}
    if (len(solo) == SOLO_THROTTLE_STREAK
            and len(_faults(solo, classes)) == len(solo)
            and len(missions) == 1):
        return (f"{len(solo)} consecutive mission-bearing {dev_type} runs failed "
                f"with {'/'.join(sorted(classes))} (latest: "
                f"{solo[0].error or 'no detail'}) — throttled to one probe; "
                f"these failures still count toward attempts")
    return None


def excusals_left(runs, run, limit: int = MAX_EXCUSALS_PER_STEP,
                  error_class: str = FAULT_CLASS) -> bool:
    """True while this (mission, mission_type) step may still be excused.

    THE escape hatch. Without it the detector livelocks: the evidence *is* the
    faults, so once armed every later fault is excused and becomes fresh
    evidence, re-arming the window forever. A permanently bad model id or a
    quota wall would then retry without limit — no give-up, no breaker, a feed
    comment per probe, and an ever-growing run store.

    Counted since the newest FINISHED run for this mission (a later step
    completing means the failing step was resolved). The give-up watermark that
    `attempt_number` also honours is deliberately NOT read here: omitting it can
    only make excusals run out sooner, which is the terminating direction.
    """
    mission = getattr(run, "mission_pmo_id", "")
    mtype = getattr(run, "mission_type", "")
    mine = [r for r in runs if getattr(r, "mission_pmo_id", "") == mission]
    anchors = [_aware(r.created_at) for r in mine if getattr(r, "state", None) == "finished"]
    since = max(anchors, default=None)
    used = sum(1 for r in mine
               if getattr(r, "mission_type", "") == mtype
               and getattr(r, "error_class", "") == error_class
               and not getattr(r, "attempt_counted", True)
               and (since is None or _aware(r.created_at) > since))
    return used < limit


def refresh_degraded(runs, degraded: dict, known: set[str],
                     classes: frozenset[str] = DEFAULT_CLASSES) -> list:
    """Recompute the shared degraded map IN PLACE and return the dev types that
    newly entered degradation (for the transition span).

    Mutating in place is mandatory — the dict identity is shared with every
    MissionManager, exactly like `shared_breakers`.

    Candidate dev types are derived from the run records and then INTERSECTED
    with `known`, the LIVE registry's dev-type names (the poll runtime unions
    `mgr.dev_types` across its managers — no registry import needed). The
    intersection is load-bearing in both directions: a renamed or removed dev
    type can never produce the two greens that clear it, so a run-derived key
    would stay degraded permanently with no way out, while the renamed type
    inherits no evidence at all. An empty `known` (no configured managers)
    therefore empties the map, which is the correct reading — nothing of that
    name is dispatchable.
    """
    fresh = {}
    for dev_type in {getattr(r, "dev_type", None) for r in runs}:
        if not dev_type or dev_type not in known:
            continue
        reason = backend_degraded(runs, dev_type, classes)
        if reason:
            fresh[dev_type] = reason
    entered = [k for k in fresh if k not in degraded]
    degraded.clear()
    degraded.update(fresh)
    return entered
