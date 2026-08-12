"""Checkpoint step-key registry (ADR-0034; 2026-08-12 audit F13).

``run.finalized_steps`` strings are the redelivery control flow — ~40 keys
that used to live as scattered literals, with TWO hand-maintained side
tables re-enumerating them (markers._SWAP_MARKER_STAGE, "MUST be registered
here"; freshness._PAST_GATE_STEPS). This registry is the one authority: a
step's key, whether it swaps the stage label (and to what), and whether its
presence means the finalize is already past the freshness gate. The side
tables are DERIVED views now — a new checkpoint author answers every
question at registration and the tables cannot drift.

Two hard rules:
- **Byte compatibility is non-negotiable.** Constants carry today's exact
  strings; record compatibility for in-flight finalizing runs at upgrade
  time depends on it (test_steps_registry pins the load-bearing values).
- **Every checkpoint key resolves to this module.** The AST guard in
  test_structure_guards scans every ``_checkpoint`` / ``finalized_steps
  .append`` call site under domain/: a bare literal or unresolvable name
  fails with remediation. The guard is anti-accident, not anti-adversary.

Leaf module: imports only ``..model`` labels — markers/freshness/every
orchestrator module may import it without cycles.

NOTE ``dynamic`` rows (families): instances are built ONLY by the
constructor functions below (``DECOMP_CHILD(i)`` …) so the guard can
recognize them; the row's ``key`` is the family PREFIX.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..model import LABEL_EXECUTE, LABEL_PLAN, LABEL_REVIEW

# tri-state sentinel: "swaps the stage label to None (removed)" is a REAL
# value in the swap table and must stay distinct from "not a swap at all"
_NOT_A_SWAP = object()


@dataclass(frozen=True)
class Step:
    key: str                          # exact key; for dynamic rows, the prefix
    dynamic: bool = False
    stage_after: object = _NOT_A_SWAP   # label str | None (removed) | sentinel
    past_freshness_gate: bool = False


# ── finalize.py ──────────────────────────────────────────────────────────────
TRANSCRIPT = "transcript"
REPLY = "reply"
TOKEN_REPORT = "token_report"
TRANSITION = "transition"

# ── transitions.py ───────────────────────────────────────────────────────────
TRANSITION_ILLEGAL = "transition:illegal"
TRANSITION_PROJECT_PARK = "transition:project_park"
TRANSITION_EXTERNAL = "transition:external"
TRANSITION_PLANNED_UPLOAD = "transition:planned:upload"
TRANSITION_PLANNED_FEED = "transition:planned:feed"
TRANSITION_PLANNED_LABELS = "transition:planned:labels"
TRANSITION_PLANNED = "transition:planned"
TRANSITION_EXECUTED_LABELS = "transition:executed:labels"
TRANSITION_EXECUTED_FEED = "transition:executed:feed"
TRANSITION_EXECUTED = "transition:executed"
TRANSITION_PLAN_NEEDED_ATTACH_UPLOAD = "transition:plan_needed_attach:upload"
TRANSITION_PLAN_NEEDED_ATTACH_FEED = "transition:plan_needed_attach:feed"
TRANSITION_PLAN_NEEDED_ATTACH_LABELS = "transition:plan_needed_attach:labels"
TRANSITION_PLAN_NEEDED_ATTACH = "transition:plan_needed_attach"
TRANSITION_PLAN_NEEDED = "transition:plan_needed"
TRANSITION_HUMAN_NEEDED = "transition:human_needed"
TRANSITION_UNKNOWN_PARK = "transition:unknown_park"

# ── review.py / completion.py / freshness.py ────────────────────────────────
REVIEW_HANDOFF = "review:handoff"
REVIEW_PR_COMMENT = "review:pr_comment"
REVIEW_FORMAL_APPROVE = "review:formal_approve"
REVIEW_FORMAL_APPROVE_OK = "review:formal_approve_ok"
REVIEW_MERGE = "review:merge"
REVIEW_DONE = "review:done"
REVIEW_CONFLICT_ROUTED = "review:conflict_routed"
REVIEW_MERGE_DEFERRED = "review:merge_deferred"
REVIEW_MERGE_FAILED = "review:merge_failed"
REVIEW_AWAITING_MERGE = "review:awaiting_merge"
REVIEW_REJECT_FEED = "review:reject:feed"
REVIEW_REJECT_PR_COMMENT = "review:reject:pr_comment"
REVIEW_REJECT_LABELS = "review:reject:labels"
REVIEW_REJECT_LOOP_WARN = "review:reject:loop_warn"
REVIEW_REJECT = "review:reject"
REVIEW_FRESHNESS_EXHAUSTED = "review:freshness_exhausted"
REVIEW_FRESHNESS_DIRECTIVE = "review:freshness_directive"
REVIEW_FRESHNESS_OK = "review:freshness_ok"
REVIEW_FRESHNESS_TRIPPED = "review:freshness_tripped"

# ── discovery.py (ADR-0033 harvest) ──────────────────────────────────────────
DISCOVERY_POST = "discovery:post"

# ── decomposition.py ─────────────────────────────────────────────────────────
DECOMP_DEPTH_LIMIT = "decomp:depth_limit"
DECOMP_CONFLICT = "decomp:conflict"
DECOMP_PARENT_NOTE = "decomp:parent_note"
DECOMP_TRACKING = "decomp:tracking"
DECOMP_CHILD_PREFIX = "decomp:child:"


def DECOMP_CHILD(i: int) -> str:
    return f"{DECOMP_CHILD_PREFIX}{i}"


def DECOMP_REL(j: int, i: int) -> str:
    return f"decomp:rel:{j}->{i}"


def DECOMP_INHERIT_IN(b: str, i: int) -> str:
    return f"decomp:inherit:in:{b}->{i}"


def DECOMP_INHERIT_OUT(i: int, dep: str) -> str:
    return f"decomp:inherit:out:{i}->{dep}"


# ── deliver.py / domain/runs.py ──────────────────────────────────────────────
DELIVER_ZIP = "deliver:zip"
ACL_USER_DELETED = "acl_user_deleted"
REPLY_STREAM_DELETED = "reply_stream_deleted"


REGISTRY: tuple[Step, ...] = (
    Step(TRANSCRIPT),
    Step(REPLY),
    Step(TOKEN_REPORT),
    Step(TRANSITION),
    Step(TRANSITION_ILLEGAL),
    Step(TRANSITION_PROJECT_PARK),
    Step(TRANSITION_EXTERNAL),
    Step(TRANSITION_PLANNED_UPLOAD),
    Step(TRANSITION_PLANNED_FEED),
    Step(TRANSITION_PLANNED_LABELS, stage_after=LABEL_EXECUTE),
    Step(TRANSITION_PLANNED),
    Step(TRANSITION_EXECUTED_LABELS, stage_after=LABEL_REVIEW),
    Step(TRANSITION_EXECUTED_FEED),
    Step(TRANSITION_EXECUTED),
    Step(TRANSITION_PLAN_NEEDED_ATTACH_UPLOAD),
    Step(TRANSITION_PLAN_NEEDED_ATTACH_FEED),
    Step(TRANSITION_PLAN_NEEDED_ATTACH_LABELS, stage_after=LABEL_EXECUTE),
    Step(TRANSITION_PLAN_NEEDED_ATTACH),
    Step(TRANSITION_PLAN_NEEDED, stage_after=LABEL_PLAN),
    Step(TRANSITION_HUMAN_NEEDED),
    Step(TRANSITION_UNKNOWN_PARK),
    Step(REVIEW_HANDOFF),
    Step(REVIEW_PR_COMMENT),
    Step(REVIEW_FORMAL_APPROVE),
    Step(REVIEW_FORMAL_APPROVE_OK),
    Step(REVIEW_MERGE, past_freshness_gate=True),
    # stage_after=None (a REAL swap-table value): REVIEW removed, mission done
    Step(REVIEW_DONE, stage_after=None, past_freshness_gate=True),
    Step(REVIEW_CONFLICT_ROUTED, stage_after=LABEL_EXECUTE,
         past_freshness_gate=True),
    # REVIEW→MERGE; MERGE is not a stage label, so the swap table sees None
    Step(REVIEW_MERGE_DEFERRED, stage_after=None, past_freshness_gate=True),
    Step(REVIEW_MERGE_FAILED, stage_after=None, past_freshness_gate=True),
    Step(REVIEW_AWAITING_MERGE, stage_after=None, past_freshness_gate=True),
    Step(REVIEW_REJECT_FEED),
    Step(REVIEW_REJECT_PR_COMMENT),
    Step(REVIEW_REJECT_LABELS, stage_after=LABEL_EXECUTE),
    Step(REVIEW_REJECT_LOOP_WARN),
    Step(REVIEW_REJECT),
    Step(REVIEW_FRESHNESS_EXHAUSTED),
    Step(REVIEW_FRESHNESS_DIRECTIVE),
    Step(REVIEW_FRESHNESS_OK),
    Step(REVIEW_FRESHNESS_TRIPPED),
    Step(DISCOVERY_POST),
    Step(DECOMP_DEPTH_LIMIT),
    Step(DECOMP_CONFLICT),
    Step(DECOMP_PARENT_NOTE),
    Step(DECOMP_TRACKING),
    Step(DECOMP_CHILD_PREFIX, dynamic=True),
    Step("decomp:rel:", dynamic=True),
    Step("decomp:inherit:in:", dynamic=True),
    Step("decomp:inherit:out:", dynamic=True),
    Step(DELIVER_ZIP),
    Step(ACL_USER_DELETED),
    Step(REPLY_STREAM_DELETED),
)

EXACT_KEYS: frozenset[str] = frozenset(
    s.key for s in REGISTRY if not s.dynamic)

# names of the family constructor functions — the AST guard accepts
# `steps.<FAMILY>(…)` call expressions as registered keys
FAMILY_CONSTRUCTORS: frozenset[str] = frozenset(
    {"DECOMP_CHILD", "DECOMP_REL", "DECOMP_INHERIT_IN", "DECOMP_INHERIT_OUT"})


def swap_marker_stage() -> dict[str, str | None]:
    """Derived view: stage label each checkpointed swap leaves behind
    (None = stage label removed). Consumed by markers._SWAP_MARKER_STAGE —
    the old hand-copy with its "MUST be registered here" comment is gone."""
    return {s.key: s.stage_after for s in REGISTRY
            if s.stage_after is not _NOT_A_SWAP}


def past_gate_steps() -> tuple[str, ...]:
    """Derived view: steps whose presence means the finalize is already past
    the freshness gate (consumed by freshness._PAST_GATE_STEPS)."""
    return tuple(s.key for s in REGISTRY if s.past_freshness_gate)
