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
with a real RunManager when a tmp path is given). Private-seam tests
(``_transition``, ``_merge_sweep``, …) remain until ADR-0015's C2/C3 retarget
them to the modules' public functions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...config import AppConfig, DevType
from ...ports.pmo import PMOPort
from ..runs import RunManager
from typing import TYPE_CHECKING as _TC

from . import (decomposition, deliver, dispatch, feed, finalize, mapper, review,
               schedule, sweeps, transitions)

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
                 internal_forge=None, skills=None):
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
        # ── advisory state (flat by design — see the module docstring) ──
        self._grace: set[str] = set()       # pmo_ids we transitioned last cycle
        self._grace_next: set[str] = set()
        # dev_type → reason (DEV_AUTH circuit breaker). Credentials are
        # DevCake-global, so main injects ONE dict shared by all managers.
        self.breakers: dict[str, str] = breakers if breakers is not None else {}
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


    # Delegating methods — implementation lives in the sibling modules
    # (ADR-0015; signatures mirror the module functions exactly). Private
    # `_x` delegators are transitional: C2/C3 retarget their callers and
    # tests to the modules' public functions, then delete them.

    # ── feed ──
    def _audit(self, pmo_id: str, action: str, detail: str = ''):
        return feed._audit(self, pmo_id, action, detail)

    def _trip_breaker(self, name: str, reason: str):
        return feed._trip_breaker(self, name, reason)

    async def _feed(self, pmo_id: str, kind: str, markdown: str, *, externalize: bool = True):
        return await feed._feed(self, pmo_id, kind, markdown, externalize=externalize)

    @staticmethod
    def _unquoted(body: str | None):
        return feed._unquoted(body)

    @staticmethod
    def _is_devcake_comment(body: str | None):
        return feed._is_devcake_comment(body)

    @staticmethod
    def _stage_of(mission: Mission):
        return feed._stage_of(mission)

    # ── schedule ──
    async def gate_map(self, missions: list[Mission]):
        return await schedule.gate_map(self, missions)

    async def schedule(self, missions: list[Mission], gate: dict[str, str] | None = None):
        return await schedule.schedule(self, missions, gate)

    async def _open_blockers(self, m: Mission, by_id: dict[str, Mission],
                             memo: dict[str, Mission | None]):
        return await schedule._open_blockers(self, m, by_id, memo)

    # ── dispatch ──
    async def dispatch(self, mission: Mission, mtype: MissionType, dev_type: DevType):
        return await dispatch.dispatch(self, mission, mtype, dev_type)

    def _identifying_prompt(self, dev_type: DevType):
        return dispatch._identifying_prompt(self, dev_type)

    def _onboard_repo_options(self, primary: str):
        return dispatch._onboard_repo_options(self, primary)

    def _decomposition_rule(self, live: Mission):
        return dispatch._decomposition_rule(self, live)

    def _reference_repos_note(self, primary: str):
        return dispatch._reference_repos_note(self, primary)

    def _protocol_spec_env(self, *, mission_id: str, mission_key: str, mission_type: str,
                           dev_type: DevType, seq: int, extra_args: str, repo, forge):
        return dispatch._protocol_spec_env(
            self, mission_id=mission_id, mission_key=mission_key,
            mission_type=mission_type, dev_type=dev_type, seq=seq,
            extra_args=extra_args, repo=repo, forge=forge)

    async def _skill_payload(self, dev_type: DevType):
        return await dispatch._skill_payload(self, dev_type)

    def runspec_secret_payload(self, run: Run):
        return dispatch.runspec_secret_payload(self, run)

    def _extra_repos_for(self, run: Run):
        return dispatch._extra_repos_for(self, run)

    def _credential_spec(self, dev_type: DevType):
        return dispatch._credential_spec(self, dev_type)

    @staticmethod
    def _derive_seq(activity):
        return dispatch._derive_seq(activity)

    @staticmethod
    def _unique_name(name: str, used: set[str]):
        return dispatch._unique_name(name, used)

    @staticmethod
    def _aware(ts: datetime):
        return dispatch._aware(ts)

    @classmethod
    def _last_giveup_at(cls, pmo_id: str):
        return dispatch._last_giveup_at(cls, pmo_id)

    def _attempt_number(self, pmo_id: str, mission_type: str, activity: Activity | None = None):
        return dispatch._attempt_number(self, pmo_id, mission_type, activity)

    async def _give_up(self, mission: Mission, mtype: MissionType, attempts: int):
        return await dispatch._give_up(self, mission, mtype, attempts)

    async def activity_payload(self, pmo_id: str, kind: str = 'issue'):
        return await dispatch.activity_payload(self, pmo_id, kind)

    async def _push_activity_repo(self, mission, mtype, seq: int):
        return await dispatch._push_activity_repo(self, mission, mtype, seq)

    def _resolve_repo(self, mission: Mission, all_runs: list | None = None):
        return dispatch._resolve_repo(self, mission, all_runs)

    def _mapper_repo(self):
        return dispatch._mapper_repo(self)

    # ── finalize ──
    async def _checkpoint(self, run: Run, key: str, fn):
        return await finalize._checkpoint(self, run, key, fn)

    async def finalize(self, run: Run, payload: dict):
        return await finalize.finalize(self, run, payload)

    def dev_failure_error(self, run: Run, payload: dict):
        return finalize.dev_failure_error(self, run, payload)

    async def restore_after_failure(self, run: Run):
        return await finalize.restore_after_failure(self, run)

    async def _post_transcript(self, run: Run, transcript: str, last_message: str | None = None):
        return await finalize._post_transcript(self, run, transcript, last_message)

    @staticmethod
    def _token_report_md(run: Run, tr: dict):
        return finalize._token_report_md(run, tr)

    # ── transitions ──
    async def _transition(self, run: Run, result: dict, plan_md: str | None):
        return await transitions._transition(self, run, result, plan_md)

    # ── review ──
    async def _flag_out_of_pipeline_merge(self, run: Run):
        return await review._flag_out_of_pipeline_merge(self, run)

    async def _conflict_attempts(self, pmo_id: str):
        return await review._conflict_attempts(self, pmo_id)

    async def _maybe_route_conflict_to_execute(self, pmo_id: str, key: str, pr_url: str,
                                               from_label: str):
        return await review._maybe_route_conflict_to_execute(self, pmo_id, key, pr_url,
                                                             from_label)

    async def _finalize_review(self, run: Run, result: dict):
        return await review._finalize_review(self, run, result)

    # ── decomposition ──
    async def _finalize_decomposition(self, run: Run, result: dict):
        return await decomposition._finalize_decomposition(self, run, result)

    # ── mapper ──
    async def dispatch_mapper(self, dev_type: DevType, missions: list[Mission]):
        return await mapper.dispatch_mapper(self, dev_type, missions)

    async def finalize_mapper(self, run: Run, payload: dict):
        return await mapper.finalize_mapper(self, run, payload)

    async def _apply_mapper_edges(self, edges: list):
        return await mapper._apply_mapper_edges(self, edges)

    @staticmethod
    def _creates_cycle(graph: dict[str, set[str]], blocker: str, blocked: str):
        return mapper._creates_cycle(graph, blocker, blocked)

    # ── sweeps ──
    async def sweeps(self, missions: list[Mission]):
        return await sweeps.sweeps(self, missions)

    async def _merge_sweep(self, m: Mission):
        return await sweeps._merge_sweep(self, m)

    async def _deferred_merge_retry(self, m: Mission, pr, pr_url: str):
        return await sweeps._deferred_merge_retry(self, m, pr, pr_url)

    async def _tracking_sweep(self, m: Mission):
        return await sweeps._tracking_sweep(self, m)

    # ── deliver ──
    async def deliver_internal_zip(self, run, pr):
        return await deliver.deliver_internal_zip(self, run, pr)

    async def deliver_internal_zip_for_mission(self, m, pr):
        return await deliver.deliver_internal_zip_for_mission(self, m, pr)

    # ── defined here (no module home yet) ──
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
        ("" / pre-v3 "main") always count: hiding them would silently reset
        attempt counters on upgrade (count, don't hide)."""
        return r.pmo_ref in ("", "main", self.instance_name)


    async def resolve_repo_live(self, mission, all_runs=None):
        """(repo_name | None, gate_reason | None), UN-GATING zero-repo missions
        onto the internal fallback forge (M11). Async — it may provision a repo.

        Order: re-register any internal repo this mission already used (so the
        sticky resolver finds it after an app restart), run the sticky resolver,
        and if that returns the specific zero-repo gate, provision an internal
        repo. Any OTHER gate (unknown marker, sticky-vanished external, mid-
        mission change) is a real gate — never silently redirected internal."""
        from ..repo_routing import REASON_ZERO_REPO
        from ...ports.internal_forge import internal_repo_name
        from ...adapters.registry import make_gitea_adapter

        from ...config import RepoInstance

        if all_runs is None:
            all_runs = self.runs.store.all()

        async def _provision() -> str:
            # ensure service accounts first (lazy retry — boot provisioning may
            # have failed against a not-yet-ready Gitea; review finding #7)
            svc = self.internal_forge.service_tokens()
            if not svc:
                await self.internal_forge.ensure_service_accounts()
                svc = self.internal_forge.service_tokens() or {}
            creds = await self.internal_forge.ensure_mission_repo(
                self.instance_name, mission.key)
            # the APP-SIDE adapter uses the devcake-app SERVICE token (org owner:
            # write:issue for PR comments + write:repository for merge), NOT the
            # mission's Dev write token (write:repository only → issue-scope 403s;
            # review finding #1). The mission's write/read pair is the Dev's,
            # delivered via runspec.
            adapter = make_gitea_adapter(creds.clone_url, svc.get("app_token"),
                                         svc.get("reviewer_token"))
            # model_construct: internal repo names carry hyphens / exceed the
            # operator-name pattern by design — they are synthesized, not input
            inst = RepoInstance.model_construct(
                name=creds.repo_name, forge="gitea", url=creds.clone_url,
                default_branch="main", api_base=None)
            self.forges.register_internal(creds.repo_name, inst, adapter)
            return creds.repo_name

        async def _ensure_registered(name: str) -> None:
            # already registered this process → no per-cycle I/O (finding #8);
            # else (re)provision — covers restart recovery + first intake
            if name not in self.forges.instances:
                await _provision()

        expected = (internal_repo_name(self.instance_name, mission.key)
                    if self.internal_forge is not None else None)

        # a done/canceled mission must never (re-)provision: the poll loop sees
        # terminal missions too, so without this guard the admin Clear endpoint
        # was silently undone within one cycle — repo, svc user, and a fresh
        # token pair resurrected (audit A4). Terminal missions are never
        # scheduled (derivation row 5), so gating them is inert.
        terminal = mission.status in ("done", "canceled")

        # restart recovery: a prior run points at this mission's internal repo,
        # but ForgeRuntime lost it on restart — re-register before resolving
        if not terminal and expected is not None and any(
                r.repo_ref == expected for r in all_runs
                if r.mission_pmo_id == mission.pmo_id):
            await _ensure_registered(expected)

        name, reason = self._resolve_repo(mission, all_runs=all_runs)
        if name is not None:
            return name, reason
        if (reason is REASON_ZERO_REPO and self.internal_forge is not None
                and not terminal):
            await _ensure_registered(expected)
            return expected, None
        return None, reason
