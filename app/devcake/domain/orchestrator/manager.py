"""MissionManager: DI container, advisory state, and the public verb surface.

Implementation lives in the sibling modules (schedule, dispatch, finalize,
transitions, review, decomposition, sweeps, feed, mapper, deliver); this class
holds explicit delegating methods with the modules' exact signatures. Binding
attributes onto the class after its definition is forbidden (ADR-0015) and
guarded by ``tests/test_structure_guards.py``.

Advisory state is FLAT on the manager by design (ADR-0009/ADR-0015): the
manager's identity is the state container — ``breakers`` is an injected dict
shared across managers, and ``build_managers()`` reconciles managers in place
on config reload precisely so this state survives. The advisory set:
``_grace``/``_grace_next``, ``breakers``, ``blocked_reasons``, ``cycles``,
``anomalies``, ``merge_handoffs``, ``rearm_merge_windows``, ``needs_human``,
``_merge_window_closed``.

Tests construct via the real ``__init__`` (``tests/fakes.make_mission_manager``
with a real RunManager when a tmp path is given) and exercise behavior through
the modules' public functions (transitions.transition, sweeps.merge_sweep,
dispatch.attempt_number, …) — the sanctioned seam per ADR-0015.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...config import AppConfig, DevType
from ...ports.pmo import PMOPort
from ..blocker_locator import LEGACY_PMO_REFS
from ..runs import RunManager
from typing import TYPE_CHECKING as _TC

from . import (deliver, dispatch, feed, finalize, mapper, review, schedule,
               sweeps)

if _TC:
    from ..forge_runtime import ForgeRuntime
from .markers import LEGAL_OUTCOMES  # noqa: F401  — public re-export

if TYPE_CHECKING:
    from datetime import datetime

    from ...ports.messaging import MessagingPort
    from ..model import Activity, Mission, MissionType
    from ..run import Run


class MissionManager:
    def __init__(self, config: AppConfig, dev_types: dict[str, DevType],
                 pmo: PMOPort, forges: "ForgeRuntime", runs: RunManager,
                 messaging: MessagingPort, *,
                 instance=None, breakers: dict[str, str] | None = None,
                 internal_forge=None, skills=None,
                 backend_degraded: dict[str, str] | None = None,
                 blocker_locator=None):
        self.config = config
        self.dev_types = dev_types
        self.pmo = pmo
        # the SHARED per-repo forge runtime (M10, docs/16 F3): repos belong
        # to the deployment, not to a PMO instance — one runtime, injected
        # into every manager; adapters resolve per run/mission
        self.forges = forges
        # the bundled internal fallback forge (M11): None until Gitea is up.
        # Zero-repo missions provision a per-mission repo here at intake.
        self.internal_forge = internal_forge
        # skill store (v1): the SkillService dispatch attaches skills from;
        # None = feature off (runs dispatch skill-less)
        self.skills = skills
        self.runs = runs
        self.messaging = messaging
        # this manager's PMO-instance identity (schema v3): one manager per
        # configured instance (ADR-0009); the name is the branch/run-id
        # prefix and the pmo_ref stamped on runs
        self.instance = instance if instance is not None else config.pmos[0]
        self.instance_name: str = self.instance.name
        # deployment-wide blocker resolution (ADR-0009 amendment): ONE shared
        # BlockerLocator holding live refs, set by build_managers on every
        # manager. REQUIRED for gate/dispatch use — None is a construction
        # window only; an unset locator fails loud at the first gate, never
        # silently degrades to single-instance resolution.
        self.blocker_locator = blocker_locator
        # ── advisory state (flat by design — see the module docstring) ──
        self._grace: set[str] = set()       # pmo_ids we transitioned last cycle
        self._grace_next: set[str] = set()
        # dev_type → reason (DEV_AUTH circuit breaker). Credentials are
        # DevCake-global, so main injects ONE dict shared by all managers.
        self.breakers: dict[str, str] = breakers if breakers is not None else {}
        # dev_type → reason (ADR-0018 backend degradation). READER ONLY here —
        # PollRuntime is the sole writer, mutating the same dict in place once
        # per cycle. Not a breaker: it throttles to one probe run and clears
        # itself on success, so it lives outside /health's circuit_breakers.
        self.backend_degraded: dict[str, str] = (
            backend_degraded if backend_degraded is not None else {})
        self.blocked_reasons: dict[str, str] = {}  # last gate_map → /health (advisory)
        self.cycles: list[list[str]] = []   # dependency cycles from the last gate_map
        self.anomalies: dict[str, str] = {}  # pmo_id → out-of-pipeline anomaly (advisory)
        # pmo_id → "awaiting human merge" note (advisory; docs/11 banner): set
        # by the merge sweep for every open-PR DEVCAKE-MERGE mission whose
        # deferred-retry window is not actively running; pruned in sweeps()
        self.merge_handoffs: dict[str, str] = {}
        # one-shot: set by the config PUT when auto_merge flips OFF→ON
        # (founder request 2026-07-15) — the next sweep opens a fresh
        # deferred-merge window for every parked DEVCAKE-MERGE mission, so
        # the flip retroactively covers the operator's existing merge queue.
        # In-memory: a restart between flip and sweep loses it (re-toggle).
        self.rearm_merge_windows: bool = False
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


    # Public verb surface + cross-cutting primitives (ADR-0015 end state).
    # Everything else is a module function taking `mgr` — the modules' public
    # functions are the sanctioned test seam.

    # ── primitives (stay methods: instance-overridable, shared substrate) ──
    def _audit(self, pmo_id: str, action: str, detail: str = ''):
        return feed._audit(self, pmo_id, action, detail)

    def _trip_breaker(self, name: str, reason: str):
        return feed._trip_breaker(self, name, reason)

    async def _feed(self, pmo_id: str, kind: str, markdown: str, *, externalize: bool = True):
        return await feed._feed(self, pmo_id, kind, markdown, externalize=externalize)

    async def _checkpoint(self, run: Run, key: str, fn):
        return await finalize._checkpoint(self, run, key, fn)

    async def _flag_out_of_pipeline_merge(self, run: Run):
        return await review._flag_out_of_pipeline_merge(self, run)

    def _attachment_cap(self) -> int:
        """The PMO's attachment size cap (deliverable zip bound)."""
        try:
            return self.pmo.capabilities().attachment_max_bytes
        except Exception:  # noqa: BLE001 — capability probe degrades to the conservative 25 MiB default cap; the zip builder still bounds the payload
            return 25 * 1024 * 1024

    def _run_is_ours(self, r) -> bool:
        """Instance-scope a run record (schema v3). Vendor pmo_ids are UUIDs for
        Linear, so the mission_pmo_id filters are already collision-free — this
        is belt-and-braces for a future PMO with colliding ids. Legacy records
        (`LEGACY_PMO_REFS` — the ONE definition, shared with the
        BlockerLocator's attribution sets so the two can never drift) always
        count: hiding them would silently reset attempt counters on upgrade
        (count, don't hide)."""
        return r.pmo_ref in LEGACY_PMO_REFS or r.pmo_ref == self.instance_name

    # ── public verbs ──
    async def gate_map(self, missions: list[Mission]):
        return await schedule.gate_map(self, missions)

    async def schedule(self, missions: list[Mission], gate: dict[str, str] | None = None):
        return await schedule.schedule(self, missions, gate)

    async def dispatch(self, mission: Mission, mtype: MissionType, dev_type: DevType):
        return await dispatch.dispatch(self, mission, mtype, dev_type)

    def runspec_secret_payload(self, run: Run):
        return dispatch.runspec_secret_payload(self, run)

    async def activity_payload(self, pmo_id: str, kind: str = 'issue'):
        return await dispatch.activity_payload(self, pmo_id, kind)

    async def resolve_repo_live(self, mission, all_runs=None):
        return await dispatch.resolve_repo_live(self, mission, all_runs)

    def mapper_repo(self):
        return dispatch.mapper_repo(self)

    async def finalize(self, run: Run, payload: dict):
        return await finalize.finalize(self, run, payload)

    def dev_failure_error(self, run: Run, payload: dict):
        return finalize.dev_failure_error(self, run, payload)

    async def restore_after_failure(self, run: Run):
        return await finalize.restore_after_failure(self, run)

    async def sweeps(self, missions: list[Mission]):
        return await sweeps.sweeps(self, missions)

    async def dispatch_mapper(self, dev_type: DevType, missions: list[Mission]):
        return await mapper.dispatch_mapper(self, dev_type, missions)

    async def finalize_mapper(self, run: Run, payload: dict):
        return await mapper.finalize_mapper(self, run, payload)

    async def deliver_internal_zip(self, run, pr):
        return await deliver.deliver_internal_zip(self, run, pr)

    async def deliver_internal_zip_for_mission(self, m, pr):
        return await deliver.deliver_internal_zip_for_mission(self, m, pr)
