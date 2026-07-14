"""MissionManager façade: deps, advisory state, and public method surface.

Implementation lives in sibling modules (ISSUES #36). Methods are bound from
those modules so `MissionManager.__new__` test patterns and attribute
monkeypatches keep working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...config import AppConfig, DevType
from ...ports.forge import ForgePort
from ...ports.pmo import PMOPort
from ..runs import RunManager
from . import (decomposition, dispatch, feed, finalize, mapper, review, schedule,
               sweeps, transitions)
from .markers import LEGAL_OUTCOMES  # noqa: F401  — public re-export

if TYPE_CHECKING:
    from ...ports.messaging import MessagingPort


class MissionManager:
    def __init__(self, config: AppConfig, dev_types: dict[str, DevType],
                 pmo: PMOPort, forge: ForgePort, runs: RunManager,
                 messaging: MessagingPort, *,
                 instance=None, breakers: dict[str, str] | None = None):
        self.config = config
        self.dev_types = dev_types
        self.pmo = pmo
        self.forge = forge
        self.runs = runs
        self.messaging = messaging
        # this manager's PMO-instance identity (schema v3): one manager per
        # configured instance (ADR-0009); the name is the branch/run-id
        # prefix and the pmo_ref stamped on runs
        self.instance = instance if instance is not None else config.pmos[0]
        self.instance_name: str = self.instance.name
        self._grace: set[str] = set()       # pmo_ids we transitioned last cycle
        self._grace_next: set[str] = set()
        # dev_type → reason (DEV_AUTH circuit breaker). Credentials are
        # DevCake-global, so main injects ONE dict shared by all managers.
        self.breakers: dict[str, str] = breakers if breakers is not None else {}
        self.forge_health: dict | None = None  # last probe result (advisory; /health)
        self.blocked_reasons: dict[str, str] = {}  # last gate_map → /health (advisory)
        self.cycles: list[list[str]] = []   # dependency cycles from the last gate_map
        self.anomalies: dict[str, str] = {}  # pmo_id → out-of-pipeline anomaly (advisory)
        # pmo_id → "awaiting human merge" note (advisory; docs/11 banner): set
        # by the merge sweep for every open-PR DEVCAKE-MERGE mission whose
        # deferred-retry window is not actively running; pruned in sweeps()
        self.merge_handoffs: dict[str, str] = {}
        # pmo_id → "needs human" note (advisory; admin Needs-Human panel).
        # Rebuilt every sweep from the DEVCAKE-NEEDS-HUMAN label — declarative,
        # restart-safe, self-pruning. Same "text — url" convention as
        # merge_handoffs so the admin UI parses both identically.
        self.needs_human: dict[str, str] = {}
        # pmo_ids whose deferred-merge window is known CLOSED (hand-off posted,
        # or no retry marker in the feed) — skips the per-cycle feed read for
        # terminally-parked missions. In-memory advisory only (PMO markers stay
        # the source of truth): repopulated after restart at one feed read per
        # parked mission; cleared when the mission leaves DEVCAKE-MERGE (the
        # documented human intervention is a label swap) or when a fresh retry
        # marker opens a new episode. A human DELETING the hand-off comment
        # instead of swapping labels isn't noticed until restart.
        self._merge_window_closed: set[str] = set()

    def rotate_grace(self) -> None:
        self._grace, self._grace_next = self._grace_next, set()


# Bind collaborator implementations (functions with `self` as first arg).
MissionManager._audit = feed._audit
MissionManager._trip_breaker = feed._trip_breaker
MissionManager._feed = feed._feed
MissionManager._unquoted = staticmethod(feed._unquoted)
MissionManager._is_devcake_comment = staticmethod(feed._is_devcake_comment)
MissionManager._stage_of = staticmethod(feed._stage_of)
MissionManager.gate_map = schedule.gate_map
MissionManager.schedule = schedule.schedule
MissionManager._open_blockers = schedule._open_blockers
MissionManager.dispatch = dispatch.dispatch
MissionManager._protocol_spec_env = dispatch._protocol_spec_env
MissionManager.runspec_secret_payload = dispatch.runspec_secret_payload
MissionManager._credential_spec = dispatch._credential_spec
MissionManager._derive_seq = staticmethod(dispatch._derive_seq)
MissionManager._unique_name = staticmethod(dispatch._unique_name)
MissionManager._aware = staticmethod(dispatch._aware)
MissionManager._last_giveup_at = classmethod(dispatch._last_giveup_at)
MissionManager._attempt_number = dispatch._attempt_number
MissionManager._give_up = dispatch._give_up
MissionManager.activity_payload = dispatch.activity_payload
MissionManager._checkpoint = finalize._checkpoint
MissionManager.finalize = finalize.finalize
MissionManager.apply_forge_health = finalize.apply_forge_health
MissionManager.dev_failure_error = finalize.dev_failure_error
MissionManager.restore_after_failure = finalize.restore_after_failure
MissionManager._post_transcript = finalize._post_transcript
MissionManager._token_report_md = staticmethod(finalize._token_report_md)
MissionManager._transition = transitions._transition
MissionManager._flag_out_of_pipeline_merge = review._flag_out_of_pipeline_merge
MissionManager._conflict_attempts = review._conflict_attempts
MissionManager._maybe_route_conflict_to_execute = review._maybe_route_conflict_to_execute
MissionManager._finalize_review = review._finalize_review
MissionManager._finalize_decomposition = decomposition._finalize_decomposition
MissionManager.dispatch_mapper = mapper.dispatch_mapper
MissionManager.finalize_mapper = mapper.finalize_mapper
MissionManager._apply_mapper_edges = mapper._apply_mapper_edges
MissionManager._creates_cycle = staticmethod(mapper._creates_cycle)
MissionManager.sweeps = sweeps.sweeps
MissionManager._merge_sweep = sweeps._merge_sweep
MissionManager._deferred_merge_retry = sweeps._deferred_merge_retry
MissionManager._tracking_sweep = sweeps._tracking_sweep


def _run_is_ours(self, r) -> bool:
    """Instance-scope a run record (schema v3). Vendor pmo_ids are UUIDs for
    Linear, so the mission_pmo_id filters are already collision-free — this
    is belt-and-braces for a future PMO with colliding ids. Legacy records
    ("" / pre-v3 "main") always count: hiding them would silently reset
    attempt counters on upgrade (count, don't hide)."""
    return r.pmo_ref in ("", "main", self.instance_name)


MissionManager._run_is_ours = _run_is_ours
