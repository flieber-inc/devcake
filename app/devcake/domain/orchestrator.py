"""Mission scheduling and finalization (docs/04): the full state machine —
ONBOARD/PLAN/EXECUTE/REVIEW dispatch, every finalization/transition, merge
sweeps, and the cross-cutting mechanics (ordering, caps, grace cycle,
compare-and-transition, attempt counting, circuit breakers).

MapperService lives in `mapper_service.py` (ISSUES #36 first cut). Further
splits (schedule / dispatch / finalize / transitions / decomposition) remain
v0.1 maintainability work once finalize checkpoints settle.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.trace import SpanKind, Status, StatusCode

from ..config import AppConfig, DevType
from ..harness import HARNESSES
from ..security import redact, redact_value
from ..telemetry import OO_ORG, OO_URL
from .model import (LABEL_CREATED, LABEL_EXECUTE, LABEL_FAILED, LABEL_MERGE,
                    LABEL_NEEDS_HUMAN, LABEL_OPTIN, LABEL_PLAN, LABEL_REVIEW,
                    LABEL_SKIP, LABEL_TRACKING, Activity, Mission, MissionRef,
                    MissionType, PRIORITY_RANK, STAGE_LABELS, derive, find_cycles)
from ..ports.forge import ForgePort, mission_branch
from ..ports.pmo import PMOPort
from .run import Run, utcnow
from .runs import RunManager

if TYPE_CHECKING:  # typing only — the domain never imports adapters at runtime
    from ..adapters.redis import Messaging

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")

# The full state machine is dispatchable, projects included (ADR-0006).
DISPATCHABLE_TYPES = {MissionType.ONBOARD, MissionType.PLAN,
                      MissionType.EXECUTE, MissionType.REVIEW}

# docs/03 §6 (normative) — the app-side trust boundary: a run may only
# transition through outcomes legal for its mission type. Devs ingest untrusted
# text (mission descriptions, human comments), so a forged outcome must never
# let an EXECUTE run approve its own work or an ONBOARD run skip REVIEW. The
# entrypoint mirrors this table, but old images may run — the app check is the
# invariant.
LEGAL_OUTCOMES: dict[str, frozenset[str]] = {
    "ONBOARD": frozenset({"plan_needed", "executed_trivially", "decomposed",
                          "human_needed"}),
    "PLAN": frozenset({"planned"}),
    "EXECUTE": frozenset({"executed", "human_needed"}),
    "REVIEW": frozenset({"reviewed", "human_needed"}),
}

STEP_MARKER = re.compile(r"`(\d+)_(ONBOARD|PLAN|EXECUTE|REVIEW)\.md`")

# Stage label each checkpointed swap leaves on the mission (None = stage label
# removed). Consulted by the redelivery external-transition check in
# _transition: a live stage matching a present marker's value is our own swap
# resuming, anything else is an external change and halts the finalize.
# Stage-label-swapping checkpoints MUST be registered here, or their
# redeliveries will misread the swap as external (cosmetic skip, safe).
_SWAP_MARKER_STAGE: dict[str, str | None] = {
    "transition:planned:labels": LABEL_EXECUTE,
    "transition:executed:labels": LABEL_REVIEW,
    "transition:executed_trivially:labels": LABEL_REVIEW,
    "transition:plan_needed_attach:labels": LABEL_EXECUTE,
    "transition:plan_needed": LABEL_PLAN,
    "review:reject:labels": LABEL_EXECUTE,
    "review:conflict_routed": LABEL_EXECUTE,
    "review:done": None,           # REVIEW removed, mission done
    "review:merge_failed": None,   # REVIEW→MERGE; MERGE is not a stage label
    "review:merge_deferred": None,
}

# docs/05 §4: feed comments longer than this are uploaded as .md attachments
# and referenced from a short comment. docs/07 §2 externalizes long bodies
# into the Dev's activity folder at the same threshold, so Devs always see
# full content either way.
FEED_INLINE_MAX = 2048

# docs/03 §4.1 — merge-failure state markers, counted/located from the feed
# so the state stays fully PMO-derivable (no local clocks or counters). The
# comments carrying them are short by construction (< FEED_INLINE_MAX): the
# markers must stay inline, never externalized to attachments. NOTE:
# get_activity reads the newest 100 comments (docs/05 §3, v0 limit) — markers
# could age out on an extremely chatty mission.
CONFLICT_MARKER = re.compile(r"`devcake:conflict-resolve:(\d+)`")
MERGE_RETRY_MARKER = "`devcake:merge-retry`"
MERGE_HANDOFF_MARKER = "`devcake:merge-handoff`"
MAX_CONFLICT_RESOLVES = 2

# Comment-provenance sentinel (docs/03 §8a, ADR-0007): every comment DevCake
# posts ends with this footer. Classification is content-based, NEVER
# author/credential-based — DevCake may post with the operator's own PMO key.
COMMENT_SENTINEL = "`devcake:v1`"
SENTINEL_RE = re.compile(r"`devcake:v1`\s*$")
DECOMPOSITION_MARKER_RE = re.compile(
    r"`devcake:decomposition:v1 parent=(\S+) manifest=([0-9a-f]{64}) "
    r"part=(\d+)/(\d+)`"
)

AUDIT_PATH = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "state" / "events.jsonl"

from .oo_auth import oo_basic_auth as _oo_basic_auth  # noqa: E402  # re-export site


class MissionManager:
    def __init__(self, config: AppConfig, dev_types: dict[str, DevType],
                 pmo: PMOPort, forge: ForgePort, runs: RunManager,
                 messaging: Messaging):
        self.config = config
        self.dev_types = dev_types
        self.pmo = pmo
        self.forge = forge
        self.runs = runs
        self.messaging = messaging
        self._grace: set[str] = set()       # pmo_ids we transitioned last cycle
        self._grace_next: set[str] = set()
        self.breakers: dict[str, str] = {}  # dev_type → reason (DEV_AUTH circuit breaker)
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

    # ── audit log (docs/10: every PMO write) ────────────────────────────────

    def _audit(self, pmo_id: str, action: str, detail: str = "") -> None:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_PATH, "a") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                "pmo_id": pmo_id, "action": action, "detail": detail}) + "\n")
        self._grace_next.add(pmo_id)
        # mirror every audit action as a span so OO alerts can fire on them
        # (ISSUES #23: `devcake_needs_human` was a file-only record no alert
        # could ever see). One span name, action as attribute — the alert set
        # queries devcake_audit_action.
        with tracer.start_as_current_span("audit.event") as span:
            span.set_attribute("devcake.audit.action", action)
            span.set_attribute("devcake.pmo.id", pmo_id)
            span.set_attribute("devcake.audit.detail", redact(detail)[:500])

    def rotate_grace(self) -> None:
        self._grace, self._grace_next = self._grace_next, set()

    def _trip_breaker(self, name: str, reason: str) -> None:
        """Single choke point for tripping a breaker: sets the in-memory dict
        AND emits a span — breakers had no telemetry at all, so the documented
        DEV_AUTH alert could never fire (ISSUES #23)."""
        self.breakers[name] = reason
        with tracer.start_as_current_span("breaker.trip") as span:
            span.set_attribute("devcake.breaker", name)
            span.set_attribute("devcake.reason", redact(reason)[:500])
            span.set_status(Status(StatusCode.ERROR, f"breaker {name} tripped"))

    # ── scheduling (docs/04 §§2–3) ───────────────────────────────────────────

    async def gate_map(self, missions: list[Mission]) -> dict[str, str]:
        """The blocked-by gate as a first-class poll artifact (docs/04 §2):
        pmo_id → human-readable reason for every open mission the gate holds
        back. Computed EVERY cycle — paused or not — so /api/v1/missions never
        serves stale gate info. Members of a dependency cycle get an explicit
        unsatisfiable-wait reason instead of ordinary blocking (docs/04 §2a).
        Also refreshes self.blocked_reasons / self.cycles (advisory mirrors)."""
        by_id = {m.pmo_id: m for m in missions}
        id_to_key = {m.pmo_id: m.key for m in missions}
        graph = {m.pmo_id: set(m.blocked_by) for m in missions
                 if m.pmo_kind == "issue" and m.blocked_by}
        cycle_of: dict[str, list[str]] = {}
        self.cycles = []
        for cyc in find_cycles(graph):
            keys = [id_to_key.get(i, i) for i in cyc]
            self.cycles.append(keys)
            for i in cyc:
                cycle_of[i] = keys
        gate: dict[str, str] = {}
        memo: dict[str, Mission | None] = {}
        for m in missions:
            if not m.blocked_by or m.status in ("done", "canceled"):
                continue
            if m.pmo_id in cycle_of:
                loop = " → ".join(cycle_of[m.pmo_id] + [cycle_of[m.pmo_id][0]])
                gate[m.pmo_id] = (f"dependency cycle: {loop} — will never "
                                  f"unblock; delete one relation in the PMO")
                continue
            open_blockers = await self._open_blockers(m, by_id, memo)
            if open_blockers:
                gate[m.pmo_id] = "blocked by " + ", ".join(open_blockers)
        self.blocked_reasons = gate
        return gate

    async def schedule(self, missions: list[Mission],
                       gate: dict[str, str] | None = None) -> int:
        if gate is None:                           # poll_loop passes its own
            gate = await self.gate_map(missions)
        candidates = []
        for m in missions:
            d = derive(m, self.config.adoption_mode)
            if not d.schedulable or d.mission_type not in DISPATCHABLE_TYPES:
                continue
            if m.pmo_id in self._grace:
                continue  # grace cycle after our own writes (docs/04 §2)
            if any(r.mission_pmo_id == m.pmo_id for r in self.runs.store.active()):
                continue  # in-flight guard
            if m.pmo_id in gate:                   # blocked-by gate (docs/04 §2)
                log.info("mission %s not scheduled — %s", m.key, gate[m.pmo_id])
                continue
            candidates.append((m, d))

        candidates.sort(key=lambda md: (PRIORITY_RANK[md[0].priority],
                                        md[0].updated_at, md[0].pmo_id))
        dispatched = 0
        active = self.runs.store.active()
        for mission, d in candidates:
            dev_type = self.dev_types.get(self.config.assignments[d.mission_type.value].dev_type)
            if (dev_type is None or dev_type.name in self.breakers
                    or "forge" in self.breakers):
                continue  # unassigned or auth breaker tripped (docs/15 §4)
            if sum(1 for r in active if r.dev_type == dev_type.name) >= dev_type.max_concurrency:
                continue
            if len(active) >= self.config.concurrency.global_max:
                break
            run = await self.dispatch(mission, d.mission_type, dev_type)
            if run:
                active.append(run)
                dispatched += 1
        return dispatched

    async def _open_blockers(self, m: Mission, by_id: dict[str, Mission],
                             memo: dict[str, Mission | None]) -> list[str]:
        """Blockers of `m` that are still open (status not done/canceled), as
        human-readable keys. A blocker we cannot read counts as open (fail-safe;
        self-heals next cycle). ADR-0007."""
        open_ = []
        for bid in m.blocked_by:
            b = by_id.get(bid)
            if b is None:
                if bid not in memo:
                    try:
                        memo[bid] = await self.pmo.get(MissionRef(bid, "issue"))
                    except Exception:
                        log.warning("blocker %s of %s unreadable — treated as open",
                                    bid, m.key)
                        memo[bid] = None
                b = memo[bid]
            if b is None:
                open_.append(f"{bid} (unreadable)")
            elif b.status not in ("done", "canceled"):
                guard = next((f" ({l})" for l in (LABEL_FAILED, LABEL_SKIP)
                              if l in b.labels), "")
                open_.append(b.key + guard)
        return open_

    # ── dispatch (docs/04 §3.1) ──────────────────────────────────────────────

    async def dispatch(self, mission: Mission, mtype: MissionType,
                       dev_type: DevType) -> Run | None:
        live = await self.pmo.get(mission.ref)                     # live re-read
        d = derive(live, self.config.adoption_mode)
        if d.mission_type != mtype:
            return None                                            # world moved on
        if live.blocked_by:
            open_blockers = await self._open_blockers(live, {}, {})  # all live
            if open_blockers:
                log.info("dispatch of %s aborted — blocked by %s",
                         live.key, ", ".join(open_blockers))
                return None

        if mission.pmo_kind == "project":
            seq = 1                       # projects only ever ONBOARD (ADR-0006)
            activity = None
        else:
            activity = await self.pmo.get_activity(mission.ref)
            seq = self._derive_seq(activity)
        # attempts restart when a human removes DEVCAKE-FAILED (docs/15 §3),
        # a later step finishes, or a human comments on the mission
        attempt = self._attempt_number(mission.pmo_id, mtype.value, activity)
        if attempt > self.config.max_attempts:
            await self._give_up(live, mtype, attempt - 1)
            return None

        assignment = self.config.assignments[mtype.value]
        from .ids import make_run_id
        run_id = make_run_id(mission.key, seq, mtype.value)

        with tracer.start_as_current_span("mission.dispatch", kind=SpanKind.PRODUCER) as span:
            span.set_attribute("devcake.run.id", run_id)
            span.set_attribute("devcake.mission.key", mission.key)
            span.set_attribute("devcake.mission.type", mtype.value)
            span.set_attribute("devcake.dev_type", dev_type.name)
            span.set_attribute("devcake.run.attempt", attempt)
            carrier: dict[str, str] = {}
            inject(carrier)
            traceparent = carrier.get("traceparent", "")

            redis_password = await self.messaging.create_run_user(run_id)
            from ..prompts import (execute_prompt, onboard_prompt, plan_prompt,
                                  review_prompt)
            repo_name = self.config.repo.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
            prompt = {
                MissionType.ONBOARD: lambda: onboard_prompt(dev_type.identifying_prompt, live),
                MissionType.PLAN: lambda: plan_prompt(dev_type.identifying_prompt, live),
                MissionType.EXECUTE: lambda: execute_prompt(
                    dev_type.identifying_prompt, live, repo_name,
                    pr_instructions=self.forge.descriptor.pr_instructions,
                    default_branch=self.config.repo.default_branch),
                MissionType.REVIEW: lambda: review_prompt(dev_type.identifying_prompt, live),
            }[mtype]()

            spec_env = self._protocol_spec_env(
                mission_id=mission.pmo_id, mission_key=mission.key,
                mission_type=mtype.value, dev_type=dev_type, seq=seq,
                extra_args=assignment.extra_cli_args)
            from .run import auth_digest
            run = Run(
                run_id=run_id, mission_key=mission.key, mission_type=mtype.value,
                pmo_kind=mission.pmo_kind,
                pmo_ref=self.config.pmo.id, repo_ref=self.config.repo.id,
                dev_type=dev_type.name, seq=seq, attempt_of_step=attempt,
                timeout_seconds=self.config.dev_timeout_minutes * 60,
                traceparent=traceparent, auth_digest=auth_digest(redis_password),
                spec_env=spec_env,
            )
            run.spec_prompt = prompt
            run.stage_label_at_dispatch = self._stage_of(live)
            run.mission_pmo_id = mission.pmo_id
            self.runs.store.save(run)                              # durable intent first

            await self.runs.executor.start(
                params={"RUN_ID": run_id,
                        "IMAGE": HARNESSES[dev_type.harness_template].image,
                        "TRACEPARENT": traceparent,
                        "REDIS_USER": f"dev-{run_id}", "REDIS_PASSWORD": redis_password},
                dag_run_id=run_id)

            if live.status == "backlog":
                await self.pmo.set_status(mission.ref, "in_progress")
                self._audit(mission.pmo_id, "set_status", "in_progress")
            log.info("dispatched %s (attempt %d, dev=%s)", run_id, attempt, dev_type.name)
            return run

    def _protocol_spec_env(self, *, mission_id: str, mission_key: str,
                           mission_type: str, dev_type: DevType, seq: int,
                           extra_args: str) -> dict[str, str]:
        """The Dev-protocol env contract (docs/07 §3), built in exactly one
        place so mission and mapper dispatches can never drift apart — a var
        missing on one path would crash the entrypoint's strict readers."""
        return {
            "DEVCAKE_MISSION_ID": mission_id,
            "DEVCAKE_MISSION_KEY": mission_key,
            "DEVCAKE_MISSION_TYPE": mission_type,
            "DEVCAKE_DEV_TYPE": dev_type.name,
            "DEVCAKE_HARNESS": dev_type.harness_template,  # app-authoritative
            "DEVCAKE_SEQ": str(seq),
            "DEVCAKE_REPO_URL": self.config.repo.url,
            "DEVCAKE_DEFAULT_BRANCH": self.config.repo.default_branch,
            "DEVCAKE_CLONE_USER": self.forge.descriptor.clone_user,
            "DEVCAKE_GIT_NAME": self.forge.descriptor.git_user_name,
            "DEVCAKE_GIT_EMAIL": self.forge.descriptor.git_email,
            "DEVCAKE_FORGE_CLI_ENVS": ",".join(self.forge.descriptor.cli_token_envs),
            "DEVCAKE_EXTRA_ARGS": extra_args,
            "DEVCAKE_MODEL": dev_type.model,
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"{OO_URL}/api/{OO_ORG}/v1/traces",
        }

    def runspec_secret_payload(self, run: Run) -> dict | None:
        """Secret half of a run spec, built from current config on request
        (docs/09 §5): nothing secret is at rest between dispatch and the Dev's
        runspec.get, and a slow container start or Redis restart cannot expire
        it. verify_auth has already authenticated the requester."""
        dt = self.dev_types.get(run.dev_type)
        if dt is None:
            return None            # dev type deleted mid-run → runspec.error
        env_creds, spec_files = self._credential_spec(dt)
        # Stage-scope forge credentials (ISSUES #15): every stage clones the
        # repo (entrypoint always git-clones), so all stages need a
        # clone-capable token. EXECUTE gets the write token (push/PR). Other
        # stages prefer token_ro when set, else fall back to the write token
        # so private repos keep working without a separate RO PAT.
        # Reviewer PAT stays app-side only.
        env: dict[str, str] = {"OTEL_EXPORTER_OTLP_BASIC": _oo_basic_auth(),
                               **env_creds}
        write = self.config.repo.token
        ro = self.config.repo.token_ro
        if run.mission_type == "EXECUTE":
            env["DEVCAKE_FORGE_TOKEN"] = write
        else:
            env["DEVCAKE_FORGE_TOKEN"] = ro or write
        return {"env": env, "credential_files": spec_files}

    async def _checkpoint(self, run: Run, key: str, fn) -> None:
        """Idempotent finalize sub-step (ISSUES #4–6): skip if already done;
        append+save only after the side effect succeeds.

        ``fn`` must be a zero-arg async callable (not a pre-created coroutine),
        so redelivery does not construct unawaited coroutines.
        """
        if key in run.finalized_steps:
            return
        await fn()
        run.finalized_steps.append(key)
        self.runs.store.save(run)

    def _credential_spec(self, dev_type: DevType) -> tuple[dict[str, str], list[dict]]:
        """Harness credentials for a run spec: requirements come from the
        harness registry, secret material from /data/secrets/{dev_type}/
        (docs/08 §4)."""
        harness = HARNESSES[dev_type.harness_template]
        env = {var: os.environ[var] for var in harness.credential_env
               if os.environ.get(var)}
        files = []
        secrets_dir = (Path(os.environ.get("DEVCAKE_DATA_DIR", "/data"))
                       / "secrets" / dev_type.name)
        for cf in harness.credential_files:
            p = secrets_dir / cf.secret_file
            if p.exists():
                files.append({"path_hint": cf.path_hint,
                              "content": p.read_text(), "mode": "600"})
            else:
                log.warning("credential file %s missing for %s — connect via OAuth "
                            "or upload it on the admin Config page", p, dev_type.name)
        return env, files

    async def _feed(self, pmo_id: str, kind: str, markdown: str) -> None:
        """The single choke-point for PMO comments: redaction + the provenance
        sentinel. Bodies over FEED_INLINE_MAX are uploaded as .md attachments
        and replaced by a short referencing comment (docs/05 §4); the sentinel
        goes on the comment, never inside the attachment, so provenance
        classification keeps working. Upload failures fall back to posting
        inline — an upload outage must never lose feed content. Projects have
        no issue-style comments API (verified live): their run artifacts
        live in the audit log + OpenObserve; the substance lands on the child
        issues anyway (ADR-0006)."""
        markdown = redact(markdown)
        if kind == "project":
            self._audit(pmo_id, "project_feed_suppressed", markdown[:120])
            return
        if len(markdown) > FEED_INLINE_MAX:
            try:
                name = f"comment-{utcnow():%Y%m%dT%H%M%S}.md"
                url = await self.pmo.upload_attachment(pmo_id, name,
                                                       markdown.encode())
                markdown = (markdown[:300].replace("\n", " ")
                            + f"… — full text attached: [{name}]({url})")
            except Exception:
                log.exception("feed attachment upload failed — posting inline")
        await self.pmo.post_feed(
            MissionRef(pmo_id, "issue"),
            markdown.rstrip() + "\n\n" + COMMENT_SENTINEL)

    @staticmethod
    def _unquoted(body: str | None) -> str:
        """Strip `>`-quoted lines: markers/sentinels inside a human's quote of
        a DevCake comment must never count as DevCake's own."""
        return "\n".join(line for line in (body or "").splitlines()
                         if not line.lstrip().startswith(">"))

    @staticmethod
    def _is_devcake_comment(body: str | None) -> bool:
        """Provenance classification (docs/03 §8a): sentinel-signed ⇒ DevCake.
        `>`-quoted lines are ignored, so a human reply that ENDS by quoting a
        DevCake comment still classifies as human — misreading a human's
        instruction as DevCake's own record is the unsafe direction."""
        return bool(SENTINEL_RE.search(
            MissionManager._unquoted(body).rstrip()))

    @staticmethod
    def _stage_of(mission: Mission) -> str | None:
        stage = mission.labels & STAGE_LABELS
        return next(iter(stage)) if stage else None

    @staticmethod
    def _derive_seq(activity) -> int:
        """docs/02 §8 — count prior step artifacts in the feed + 1."""
        steps = [int(m.group(1)) for e in activity.entries
                 for m in STEP_MARKER.finditer(e.body or "")]
        return (max(steps) + 1) if steps else 1

    @staticmethod
    def _unique_name(name: str, used: set[str]) -> str:
        """docs/07 §2 collision rule: later duplicates get -2, -3, … suffixes."""
        stem, dot, ext = name.rpartition(".")
        cand, i = name, 1
        while cand in used:
            i += 1
            cand = f"{stem}-{i}.{ext}" if dot else f"{name}-{i}"
        used.add(cand)
        return cand

    @staticmethod
    def _aware(ts: datetime) -> datetime:
        """Anchor timestamps come from three sources (audit log, run records,
        PMO comments); a stray naive one must not crash the scheduler."""
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    @classmethod
    def _last_giveup_at(cls, pmo_id: str) -> datetime | None:
        try:
            ts = None
            with open(AUDIT_PATH) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        if e.get("pmo_id") == pmo_id \
                                and e.get("action") == "devcake_failed":
                            ts = cls._aware(datetime.fromisoformat(e["ts"]))
                    except Exception:
                        continue  # one bad audit line must never halt scheduling
            return ts
        except FileNotFoundError:
            return None

    def _attempt_number(self, pmo_id: str, mission_type: str,
                        activity: Activity | None = None) -> int:
        """Count consecutive counted failures, independent of transcript seq.

        The count resets at the newest of: the last give-up event, ANY finished
        run for this mission (a later step finishing implies earlier failures
        were resolved), or the latest human feed comment (a human touching the
        mission is an intervention — the step deserves fresh attempts)."""
        all_runs = [r for r in self.runs.store.all() if r.mission_pmo_id == pmo_id]
        history = [r for r in all_runs if r.mission_type == mission_type]
        anchors = [t for t in [self._last_giveup_at(pmo_id),
                               *(self._aware(r.created_at) for r in all_runs
                                 if r.state == "finished")] if t]
        if activity is not None:
            anchors += [self._aware(e.ts) for e in activity.entries
                        if e.kind == "comment"
                        and not self._is_devcake_comment(e.body)]
        since = max(anchors, default=None)
        ignored = ("DEV_AUTH", "DEV_FORGE_AUTH", "dev failure artifact (exit 13)")
        return 1 + sum(
            1 for r in history
            if r.state in ("failed", "timed_out", "orphaned")
            and not any(marker in (r.error or "") for marker in ignored)
            and (since is None or self._aware(r.created_at) > since)
        )

    async def _give_up(self, mission: Mission, mtype: MissionType, attempts: int) -> None:
        if LABEL_FAILED in mission.labels:
            return
        with tracer.start_as_current_span("mission.give_up") as span:
            span.set_attribute("devcake.mission.key", mission.key)
            span.set_attribute("devcake.mission.type", mtype.value)
            span.set_attribute("devcake.run.attempt", attempts)
            span.set_status(Status(StatusCode.ERROR,
                                   f"gave up after {attempts} attempts"))
            await self.pmo.swap_labels(mission.ref, remove=set(), add={LABEL_FAILED})
            await self._feed(
                mission.pmo_id, mission.pmo_kind,
                f"⚠️ **DevCake gave up on this mission's {mtype.value} step** after "
                f"{attempts} failed attempts. Remove the `DEVCAKE-FAILED` label to retry. "
                f"(Traces: search run ids `{mission.key}-*` in OpenObserve.)")
            self._audit(mission.pmo_id, "devcake_failed", mtype.value)
        log.warning("DEVCAKE-FAILED applied to %s (%s)", mission.key, mtype.value)

    # ── finalization (docs/04 §4) ────────────────────────────────────────────

    async def finalize(self, run: Run, payload: dict) -> None:
        result = payload.get("result") or {}
        outcome = result.get("outcome", "")
        transcript = payload.get("transcript_md", "")
        token_report = payload.get("token_report") or {}
        plan_md = payload.get("plan_md")
        pmo_id = run.mission_pmo_id

        ctx = None
        if run.traceparent:
            from opentelemetry.propagate import extract
            ctx = extract({"traceparent": run.traceparent})
        with tracer.start_as_current_span("run.finalize", context=ctx,
                                          kind=SpanKind.CONSUMER) as span:
            span.set_attribute("devcake.run.id", run.run_id)
            span.set_attribute("devcake.outcome", outcome)
            for k in ("input_tokens", "output_tokens", "total_tokens"):
                if token_report.get(k) is not None:
                    span.set_attribute(f"devcake.tokens.{k.removesuffix('_tokens')}",
                                       token_report[k])
            if token_report.get("cost_usd") is not None:
                span.set_attribute("devcake.cost.usd", token_report["cost_usd"])

            # 1 — transcript (idempotent via finalized_steps)
            if "transcript" not in run.finalized_steps:
                await self._post_transcript(run, transcript)
                run.finalized_steps.append("transcript")
                self.runs.store.save(run)

            run.token_report = redact_value(token_report)  # persisted cost source
            # 2 — token report (INV-5: always)
            if "token_report" not in run.finalized_steps:
                await self._feed(pmo_id, run.pmo_kind,
                                 self._token_report_md(run, token_report))
                run.finalized_steps.append("token_report")
                self.runs.store.save(run)

            # failure artifact (docs/07 §4 nonzero exits): evidence posted above,
            # NO transition — and the dispatch-time status write is REVERTED so the
            # mission re-derives exactly as before the attempt (INV-3; without this,
            # a failed first ONBOARD strands the mission at in_progress/no-label = row 9)
            if not outcome:
                exit_code = payload.get("exit_code")
                await self.messaging.delete_run_user(run.run_id)
                await self.messaging.delete_reply_stream(run.run_id)
                run.result = None
                run.state = "failed"
                run.error = self._dev_failure_error(run, payload)
                run.ended_at = utcnow()
                self.runs.store.save(run)
                span.set_attribute("devcake.verdict", f"failed: {run.error}")
                span.set_status(Status(StatusCode.ERROR, run.error))
                await self.restore_after_failure(run)
                log.warning("run %s failed (exit %s, attempt %d)",
                            run.run_id, exit_code, run.attempt_of_step)
                return

            # 3 — compare-and-transition. A ValueError from a transition means
            # the Dev's payload was structurally invalid (e.g. malformed
            # decomposition / bad blocked_by) — that is DEV_BAD_OUTPUT, a
            # counted attempt (docs/15 §2), NOT an exception to propagate: the
            # run must fail cleanly so the mission reschedules next cycle
            # instead of stranding in `finalizing` until the watchdog timeout.
            if "transition" not in run.finalized_steps:
                try:
                    await self._transition(run, result, plan_md)
                except ValueError as e:
                    await self.messaging.delete_run_user(run.run_id)
                    await self.messaging.delete_reply_stream(run.run_id)
                    run.result = redact_value(result)
                    run.state = "failed"
                    run.error = redact(f"DEV_BAD_OUTPUT: {e}")
                    run.ended_at = utcnow()
                    self.runs.store.save(run)
                    span.set_attribute("devcake.verdict", f"failed: {run.error}")
                    span.set_status(Status(StatusCode.ERROR, run.error))
                    await self.restore_after_failure(run)
                    log.warning("run %s failed with DEV_BAD_OUTPUT: %s",
                                run.run_id, e)
                    return
                run.finalized_steps.append("transition")
                self.runs.store.save(run)

            await self.messaging.delete_run_user(run.run_id)
            await self.messaging.delete_reply_stream(run.run_id)
            run.result = redact_value(result)
            run.state, run.ended_at = "finished", utcnow()
            self.runs.store.save(run)
            # app-level judgment onto the trace: Dagu can report the step green
            # while _transition refused to act — make that visible in OO
            span.set_attribute("devcake.verdict", run.verdict or "success")
            if run.verdict and not run.verdict.startswith("handed off"):
                span.set_status(Status(StatusCode.ERROR, run.verdict))
                span.add_event("devcake.verdict", {"detail": run.verdict})
            log.info("finalized %s (%s)", run.run_id, outcome)

    def apply_forge_health(self, data: dict) -> None:
        """Single writer for forge_health + the global forge breaker: latch only
        on a definitive credential/permission failure, clear only on success —
        a transient probe failure must neither latch nor clear."""
        if data.get("ok"):
            self.forge_health = data
            self.breakers.pop("forge", None)
        elif data.get("transient"):
            self.forge_health = data
            log.warning("forge probe transient failure (breaker untouched): %s",
                        data.get("detail"))
            # span-mirrored so the FORGE_TRANSIENT >15m alert has a signal
            # (ISSUES #23) — the log line above never reaches the traces stream
            with tracer.start_as_current_span("forge.probe_transient") as span:
                span.set_attribute("devcake.reason",
                                   redact(str(data.get("detail") or ""))[:500])
        else:
            self.forge_health = data
            self._trip_breaker("forge",
                               data.get("detail") or "repository is not writable")

    def _dev_failure_error(self, run: Run, payload: dict) -> str:
        exit_code = payload.get("exit_code")
        if exit_code == 12:
            self._trip_breaker(run.dev_type, f"auth failure in {run.run_id}")
            return "DEV_AUTH (does not count toward attempts; breaker tripped)"
        detail = redact(str(payload.get("error_detail") or ""))
        detail = " ".join(detail.split())[:500]
        error_class = str(payload.get("error_class") or "")
        auth_markers = ("403", "401", "authentication failed", "repository not found",
                        "write access to repository not granted")
        if exit_code == 13 and (error_class == "DEV_FORGE_AUTH"
                                or any(marker in detail.lower()
                                       for marker in auth_markers)):
            # only the structured Dev classification may latch the global
            # breaker — a bare "403"/"401" substring can be a rate limit or an
            # incidental URL fragment; the poll-loop re-probe clears mistakes
            if error_class == "DEV_FORGE_AUTH":
                self._trip_breaker(
                    "forge", f"repository credential rejected in {run.run_id}")
            return "DEV_FORGE_AUTH: " + (detail or "repository credential rejected")
        if exit_code == 13:
            return "DEV_FORGE: " + (detail or "clone/push setup failed")
        return f"dev failure artifact (exit {exit_code})"

    async def _transition(self, run: Run, result: dict, plan_md: str | None) -> None:
        outcome = result.get("outcome", "")
        pmo_id = run.mission_pmo_id
        if outcome not in LEGAL_OUTCOMES.get(run.mission_type, frozenset()):
            # illegal (or unknown) outcome for this step — the trust boundary.
            # Park for a human; never act on it (docs/03 §6, docs/15).
            async def _illegal():
                await self.pmo.swap_labels(MissionRef(pmo_id, run.pmo_kind),
                                       remove=set(), add={LABEL_SKIP})
                await self._feed(
                    pmo_id, run.pmo_kind,
                    f"⛔ DevCake received outcome `{outcome or '(empty)'}` from a "
                    f"**{run.mission_type}** run — not a legal outcome for that step. "
                    f"No transition was applied; parked with `DEVCAKE-SKIP` for a "
                    f"human to inspect.")
                self._audit(pmo_id, "illegal_outcome",
                            f"{outcome or '(empty)'} from {run.mission_type}")
            await self._checkpoint(run, "transition:illegal", _illegal)
            run.verdict = redact(
                f"rejected: outcome {outcome or '(empty)'} is illegal "
                f"for {run.mission_type} — parked with DEVCAKE-SKIP")
            log.warning("illegal outcome %r from %s run %s — parked",
                        outcome, run.mission_type, run.run_id)
            return
        live = await self.pmo.get(MissionRef(pmo_id, run.pmo_kind))               # live re-read
        if run.pmo_kind == "project" and outcome not in ("decomposed", "human_needed"):
            async def _proj_park():
                await self.pmo.swap_labels(MissionRef(pmo_id, "project"),
                                           remove=set(), add={LABEL_SKIP})
                self._audit(pmo_id, "project_bad_outcome_parked", outcome)
            await self._checkpoint(run, "transition:project_park", _proj_park)
            run.verdict = redact(
                f"rejected: project returned {outcome} (only decomposed "
                f"is legal) — parked with DEVCAKE-SKIP")
            log.warning("project %s returned %s (only decomposed is legal) — parked",
                        run.mission_key, outcome)
            return
        # A redelivery must not mistake its OWN checkpointed label swap for an
        # external change — but a change a human made BETWEEN deliveries (e.g.
        # canceling the mission while the app was down) must still halt the
        # resumed finalize. So: the live stage is acceptable iff it equals the
        # dispatch stage OR the exact stage a checkpointed swap of ours left
        # behind (_SWAP_MARKER_STAGE). Markers that never touch stage labels
        # (feeds, uploads, pr comments) deliberately contribute nothing, and
        # unmapped future markers degrade to the strict pre-swap check, never
        # to suppression.
        expected_stages = {run.stage_label_at_dispatch}
        expected_stages.update(
            stage for marker, stage in _SWAP_MARKER_STAGE.items()
            if marker in run.finalized_steps)
        if self._stage_of(live) not in expected_stages:
            async def _external():
                await self._feed(
                    pmo_id, run.pmo_kind,
                    f"ℹ️ DevCake completed a **{run.mission_type}** run (`{run.run_id}`), but "
                    f"this mission's state was changed externally while it ran. The output is "
                    f"posted above; **no status or label changes were applied**.")
                self._audit(pmo_id, "external_transition", run.run_id)
            await self._checkpoint(run, "transition:external", _external)
            run.verdict = ("skipped: mission state changed externally while the "
                           "run was in flight — no transition applied")
            log.warning("EXTERNAL_TRANSITION on %s — no labels applied", run.run_id)
            return

        if outcome in ("executed", "executed_trivially", "reviewed"):
            await self._flag_out_of_pipeline_merge(run)

        if outcome == "planned":
            plan_name = f"PLAN_{run.seq}.md"
            plan_url_box: list[str] = []

            async def _plan_upload():
                url = await self.pmo.upload_attachment(
                    pmo_id, plan_name, redact(plan_md or "").encode())
                plan_url_box.clear()
                plan_url_box.append(url)

            async def _plan_feed():
                url = plan_url_box[0] if plan_url_box else f"(see attachment {plan_name})"
                await self._feed(
                    pmo_id, run.pmo_kind,
                    f"📋 DevCake plan for this mission: [{plan_name}]({url})")

            async def _plan_labels():
                await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                           remove={LABEL_PLAN}, add={LABEL_EXECUTE})
                self._audit(pmo_id, "label_swap", f"{LABEL_PLAN}→{LABEL_EXECUTE}")

            await self._checkpoint(run, "transition:planned:upload", _plan_upload)
            await self._checkpoint(run, "transition:planned:feed", _plan_feed)
            await self._checkpoint(run, "transition:planned:labels", _plan_labels)
            if "transition:planned" not in run.finalized_steps:
                run.finalized_steps.append("transition:planned")
                self.runs.store.save(run)
        elif outcome == "executed":
            async def _executed_labels():
                await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                           remove={LABEL_EXECUTE}, add={LABEL_REVIEW})
                self._audit(pmo_id, "label_swap", f"{LABEL_EXECUTE}→{LABEL_REVIEW}")

            async def _executed_feed():
                await self._feed(
                    pmo_id, run.pmo_kind,
                    f"🔀 DevCake opened/updated the pull request: "
                    f"{result.get('pr_url', '(no url reported)')} — awaiting REVIEW.")

            await self._checkpoint(run, "transition:executed:labels", _executed_labels)
            await self._checkpoint(run, "transition:executed:feed", _executed_feed)
            if "transition:executed" not in run.finalized_steps:
                run.finalized_steps.append("transition:executed")
                self.runs.store.save(run)
        elif outcome == "executed_trivially":
            async def _trivial_labels():
                await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                           remove=set(), add={LABEL_REVIEW})
                self._audit(pmo_id, "label_add", LABEL_REVIEW)

            async def _trivial_feed():
                await self._feed(
                    pmo_id, run.pmo_kind,
                    f"🔀 Trivial path: PR opened ({result.get('pr_url', '?')}) — "
                    f"the trivial path never skips REVIEW (docs/03 §1.1).")

            await self._checkpoint(run, "transition:executed_trivially:labels",
                                   _trivial_labels)
            await self._checkpoint(run, "transition:executed_trivially:feed",
                                   _trivial_feed)
            if "transition:executed_trivially" not in run.finalized_steps:
                run.finalized_steps.append("transition:executed_trivially")
                self.runs.store.save(run)
        elif outcome == "reviewed":
            await self._finalize_review(run, result)
        elif outcome == "decomposed":
            await self._finalize_decomposition(run, result)
        elif outcome == "plan_needed" and plan_md:
            plan_name = f"PLAN_{run.seq}.md"
            plan_url_box: list[str] = []

            async def _plan_attach_upload():
                url = await self.pmo.upload_attachment(
                    pmo_id, plan_name, redact(plan_md).encode())
                plan_url_box.clear()
                plan_url_box.append(url)

            async def _plan_attach_feed():
                url = plan_url_box[0] if plan_url_box else f"(see {plan_name})"
                await self._feed(
                    pmo_id, run.pmo_kind,
                    f"📋 DevCake attached an opportunistic plan from triage: "
                    f"[{plan_name}]({url}) — skipping the PLAN step.")

            async def _plan_attach_labels():
                await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                           remove=set(), add={LABEL_EXECUTE})
                self._audit(pmo_id, "label_add", LABEL_EXECUTE)

            await self._checkpoint(run, "transition:plan_needed_attach:upload",
                                   _plan_attach_upload)
            await self._checkpoint(run, "transition:plan_needed_attach:feed",
                                   _plan_attach_feed)
            await self._checkpoint(run, "transition:plan_needed_attach:labels",
                                   _plan_attach_labels)
            if "transition:plan_needed_attach" not in run.finalized_steps:
                run.finalized_steps.append("transition:plan_needed_attach")
                self.runs.store.save(run)
        elif outcome == "plan_needed":
            async def _plan_needed():
                await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                           remove=set(), add={LABEL_PLAN})
                self._audit(pmo_id, "label_add", LABEL_PLAN)
            await self._checkpoint(run, "transition:plan_needed", _plan_needed)
        elif outcome == "human_needed":
            # deliberate hand-off (docs/03 §4a, ADR-0007): the run finished
            # cleanly, so it never counts toward max_attempts; the stage label
            # stays so work resumes at the same step once the human removes
            # DEVCAKE-NEEDS-HUMAN. Checkpointed so redelivery never re-posts
            # the baton (ISSUES #5).
            async def _human():
                await self.pmo.swap_labels(MissionRef(pmo_id, run.pmo_kind),
                                           remove=set(), add={LABEL_NEEDS_HUMAN})
                if run.stage_label_at_dispatch is None and live.status == "in_progress":
                    await self.pmo.set_status(MissionRef(pmo_id, run.pmo_kind), "backlog")
                    self._audit(pmo_id, "set_status", "backlog (human hand-off)")
                nth = 1 + sum(
                    1 for r in self.runs.store.all()
                    if r.mission_pmo_id == pmo_id and r.state == "finished"
                    and (r.result or {}).get("outcome") == "human_needed"
                    and r.stage_label_at_dispatch == run.stage_label_at_dispatch)
                warn = "" if nth < 2 else (
                    f"⚠️ **Hand-off #{nth} on this step.** If DevCake keeps returning "
                    f"here, the mission may need re-scoping — add `DEVCAKE-SKIP` to "
                    f"stop DevCake on it.\n\n")
                baton = (f"{warn}✋ **DevCake needs a human.** "
                         f"{result.get('summary', '(no details reported)')}\n\n"
                         f"When resolved, remove the `DEVCAKE-NEEDS-HUMAN` label and "
                         f"DevCake resumes where it left off.")
                await self._feed(pmo_id, run.pmo_kind, baton)
                if run.pmo_kind == "project":
                    try:
                        await self.pmo.post_feed(
                            MissionRef(pmo_id, "project"),
                            redact(baton) + "\n\n" + COMMENT_SENTINEL)
                    except Exception:
                        log.warning("project-update hand-off failed for %s — "
                                    "summary lives in the audit log only",
                                    run.mission_key, exc_info=True)
                self._audit(pmo_id, "devcake_needs_human",
                            f"#{nth}: " + (result.get("summary") or "")[:110])
            await self._checkpoint(run, "transition:human_needed", _human)
            run.verdict = f"handed off: needs human on {run.mission_type}"
            self.needs_human[pmo_id] = (
                f"{run.mission_key}: needs human on {run.mission_type}"
                + (f" — {live.url}" if live.url else ""))
        else:
            async def _unknown():
                await self.pmo.swap_labels(MissionRef(pmo_id, run.pmo_kind),
                                           remove=set(), add={LABEL_SKIP})
                await self._feed(
                    pmo_id, run.pmo_kind,
                    f"ℹ️ DevCake received unknown outcome `{outcome}` — parked with "
                    f"`DEVCAKE-SKIP` for a human to inspect.")
                self._audit(pmo_id, "label_add",
                            f"{LABEL_SKIP} (unknown outcome {outcome})")
            await self._checkpoint(run, "transition:unknown_park", _unknown)
            run.verdict = redact(
                f"rejected: unknown outcome {outcome} — parked with DEVCAKE-SKIP")

    async def _flag_out_of_pipeline_merge(self, run: Run) -> None:
        """Detection tripwire (docs/14, ADR-0007 addendum): the Dev's forge token
        can merge unless branch protection forbids it. If the mission's PR turns
        up merged while the mission is still mid-pipeline, say so loudly —
        detection only; a human decides (they may have merged early themselves)."""
        try:
            pr = await self.forge.get_pr_by_branch(mission_branch(run.mission_key))
            if not pr:
                return
            state = await self.forge.pr_state(pr.number)
            if not state.merged:
                return
            await self._feed(
                run.mission_pmo_id, run.pmo_kind,
                f"⚠️ **Out-of-pipeline merge detected:** {state.url} is already "
                f"merged, but this mission is still mid-pipeline "
                f"({run.mission_type}). If you merged it yourself on purpose, "
                f"mark the mission Done (or add `DEVCAKE-SKIP`); otherwise check "
                f"who merged it — DevCake did not.")
            self._audit(run.mission_pmo_id, "out_of_pipeline_merge", state.url)
            self.anomalies[run.mission_pmo_id] = (
                f"{run.mission_key}: PR merged outside the pipeline ({state.url})")
            log.warning("out-of-pipeline merge on %s (%s)", run.mission_key,
                        state.url)
        except Exception:
            log.debug("out-of-pipeline merge check failed for %s",
                      run.mission_key, exc_info=True)

    # ── REVIEW finalization (docs/03 §4, merge-before-Done) ──────────────────

    async def _conflict_attempts(self, pmo_id: str) -> int:
        """Prior auto-resolve attempts, derived from feed markers — the PMO is
        the source of truth (docs/03), so the count survives restarts and a
        human deleting directive comments deliberately resets it."""
        act = await self.pmo.get_activity(MissionRef(pmo_id, "issue"))
        hits = [int(mt.group(1)) for e in act.entries
                for mt in CONFLICT_MARKER.finditer(self._unquoted(e.body))]
        return max(hits) if hits else 0

    async def _maybe_route_conflict_to_execute(self, pmo_id: str, key: str,
                                               pr_url: str,
                                               from_label: str) -> bool:
        """docs/03 §4.1 — on an auto-resolvable merge failure (conflict or
        stale branch), route the mission back to EXECUTE with a resolve
        directive, max MAX_CONFLICT_RESOLVES attempts per mission. Returns
        True when routed; any failure or decline returns False so the caller's
        human fallback (DEVCAKE-MERGE) is never blocked."""
        if not self.config.auto_resolve_merge_conflicts:
            return False
        try:
            n = await self._conflict_attempts(pmo_id)
            if n >= MAX_CONFLICT_RESOLVES:
                self._audit(pmo_id, "conflict_resolve_exhausted", pr_url)
                return False
            # directive FIRST, then swap (mirrors the reject path): if the
            # post fails the mission stays put and the marker count never
            # undercounts. The EXECUTE playbook tells the Dev a 🧩 resolve
            # directive overrides its normal implement-the-mission job (🧩 is
            # reserved for this directive — 🔀 already means "PR opened").
            await self._feed(
                pmo_id, "issue",
                f"🧩 Auto-merge hit a merge conflict on {pr_url} (auto-resolve "
                f"attempt {n + 1}/{MAX_CONFLICT_RESOLVES}) — back to EXECUTE. "
                f"Next Dev: sync `{mission_branch(key)}` with the default branch, "
                f"resolve the conflicts, and push; the PR then returns to "
                f"REVIEW. `devcake:conflict-resolve:{n + 1}`")
            await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                       remove={from_label}, add={LABEL_EXECUTE})
            self._audit(pmo_id, "conflict_resolve_dispatched",
                        f"attempt {n + 1} ({pr_url})")
            return True
        except Exception:
            log.exception("conflict auto-resolve routing failed for %s", key)
            return False

    async def _finalize_review(self, run: Run, result: dict) -> None:
        pmo_id = run.mission_pmo_id
        verdict = result.get("verdict")
        report = result.get("report_md") or result.get("summary") or ""
        pr = await self.forge.get_pr_by_branch(mission_branch(run.mission_key))
        pr_url = (pr.url if pr else None) or result.get("pr_url") or "?"
        footer = self.forge.approval_footer(pr_url)

        if verdict == "approve":
            formal = False
            if pr:
                async def _pr_comment():
                    await self.forge.post_pr_comment(
                        pr.number,
                        "## DevCake REVIEW: APPROVED-BY-DEVCAKE ✅\n\n"
                        + report + footer)
                await self._checkpoint(run, "review:pr_comment", _pr_comment)

                async def _formal():
                    try:
                        return await self.forge.approve(pr.number)
                    except Exception:
                        log.exception("formal approval failed — falling back to marker")
                        return False
                if "review:formal_approve" not in run.finalized_steps:
                    formal = await _formal()
                    run.finalized_steps.append("review:formal_approve")
                    if formal:
                        run.finalized_steps.append("review:formal_approve_ok")
                    self.runs.store.save(run)
                else:
                    # redelivery: only claim formal approval if it succeeded
                    formal = "review:formal_approve_ok" in run.finalized_steps

            if self.config.auto_merge and pr:
                if "review:done" not in run.finalized_steps \
                        and "review:merge_failed" not in run.finalized_steps \
                        and "review:merge_deferred" not in run.finalized_steps \
                        and "review:conflict_routed" not in run.finalized_steps:
                    try:
                        async def _merge():
                            await self.forge.merge(pr.number)
                        await self._checkpoint(run, "review:merge", _merge)
                        async def _done():
                            await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                                 remove={LABEL_REVIEW}, add=set())
                            await self.pmo.set_status(MissionRef(pmo_id, "issue"), "done")
                            await self._feed(
                                pmo_id, "issue",
                                f"✅ REVIEW approved; PR merged ({pr_url}). "
                                f"Mission done.")
                            self._audit(pmo_id, "review_approve_merged", pr_url)
                        await self._checkpoint(run, "review:done", _done)
                    except Exception as e:
                        # Already-merged treated as success by forge.merge; if we
                        # still fail, re-probe before posting merge-failed (ISSUES #6).
                        try:
                            state = await self.forge.pr_state(pr.number)
                            if state.merged:
                                async def _done_merged():
                                    if "review:merge" not in run.finalized_steps:
                                        run.finalized_steps.append("review:merge")
                                        self.runs.store.save(run)
                                    await self.pmo.swap_labels(
                                        MissionRef(pmo_id, "issue"),
                                        remove={LABEL_REVIEW}, add=set())
                                    await self.pmo.set_status(
                                        MissionRef(pmo_id, "issue"), "done")
                                    await self._feed(
                                        pmo_id, "issue",
                                        f"✅ REVIEW approved; PR merged ({pr_url}). "
                                        f"Mission done.")
                                    self._audit(pmo_id, "review_approve_merged", pr_url)
                                await self._checkpoint(run, "review:done", _done_merged)
                                return
                        except Exception:
                            log.exception("pr_state probe after merge fail for %s",
                                          pr_url)
                        mstate = None
                        try:
                            mstate = await self.forge.mergeable(pr.number)
                        except Exception:
                            log.exception("mergeable check failed for %s", pr_url)
                        if mstate is False:
                            if "review:conflict_routed" in run.finalized_steps:
                                return
                            try:
                                routed = await self._maybe_route_conflict_to_execute(
                                    pmo_id, run.mission_key, pr_url, LABEL_REVIEW)
                            except Exception:
                                log.exception("conflict route failed for %s",
                                              run.mission_key)
                                routed = False
                            if routed:
                                run.finalized_steps.append("review:conflict_routed")
                                self.runs.store.save(run)
                                return
                        if "review:merge_failed" not in run.finalized_steps \
                                and "review:merge_deferred" not in run.finalized_steps:
                            async def _fail_path():
                                await self.pmo.swap_labels(
                                    MissionRef(pmo_id, "issue"),
                                    remove={LABEL_REVIEW}, add={LABEL_MERGE})
                                if mstate is not False and \
                                        self.config.merge_retry_window_minutes > 0:
                                    await self._feed(
                                        pmo_id, "issue",
                                        f"⏳ REVIEW approved but the merge is not "
                                        f"possible yet ({e}) — DevCake keeps "
                                        f"retrying for up to "
                                        f"{self.config.merge_retry_window_minutes} "
                                        f"minutes (mergeability computing / CI "
                                        f"pipeline running). You can merge {pr_url} "
                                        f"manually at any time. {MERGE_RETRY_MARKER}")
                                    self._audit(pmo_id, "merge_deferred",
                                                str(e)[:120])
                                    self._merge_window_closed.discard(pmo_id)
                                    run.finalized_steps.append(
                                        "review:merge_deferred")
                                else:
                                    await self._feed(
                                        pmo_id, "issue",
                                        f"⚠️ REVIEW approved but auto-merge failed "
                                        f"({e}); awaiting human merge of {pr_url} "
                                        f"(`DEVCAKE-MERGE`). {MERGE_HANDOFF_MARKER}")
                                    self._audit(pmo_id,
                                                "review_approve_merge_failed",
                                                str(e)[:120])
                                    run.finalized_steps.append(
                                        "review:merge_failed")
                                self.runs.store.save(run)
                            await _fail_path()
            elif "review:awaiting_merge" not in run.finalized_steps:
                async def _await_merge():
                    await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                               remove={LABEL_REVIEW}, add={LABEL_MERGE})
                    await self._feed(
                        pmo_id, "issue",
                        f"✅ REVIEW approved "
                        f"({'formal approval filed' if formal else 'APPROVED-BY-DEVCAKE marker'}). "
                        f"Awaiting human merge of {pr_url} — the merge sweep completes "
                        f"this mission once it merges." + footer)
                    self._audit(pmo_id, "review_approve_awaiting_merge", pr_url)
                await self._checkpoint(run, "review:awaiting_merge", _await_merge)
        else:  # reject
            rejections = 1 + sum(
                1 for r in self.runs.store.all()
                if r.mission_pmo_id == pmo_id and r.mission_type == "REVIEW"
                and r.state == "finished"
                and (r.result or {}).get("verdict") == "reject")

            async def _reject_feed():
                feed_body = (f"🔁 REVIEW rejected (round {rejections}) — back to "
                             f"EXECUTE.\n\n" + report)
                if report:
                    try:
                        name = f"{run.seq}_REVIEW_REPORT.md"
                        url = await self.pmo.upload_attachment(
                            pmo_id, name, redact(report).encode())
                        feed_body = (
                            f"🔁 REVIEW rejected (round {rejections}) — back "
                            f"to EXECUTE. Full report attached: "
                            f"[{name}]({url})")
                    except Exception:
                        log.exception("review report upload failed — posting inline")
                await self._feed(pmo_id, "issue", feed_body)

            async def _reject_pr_comment():
                if pr:
                    await self.forge.post_pr_comment(
                        pr.number,
                        "## DevCake REVIEW: changes requested 🔁\n\n"
                        + report + footer)

            async def _reject_labels():
                await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                       remove={LABEL_REVIEW}, add={LABEL_EXECUTE})
                self._audit(pmo_id, "label_swap",
                            f"{LABEL_REVIEW}→{LABEL_EXECUTE}")

            async def _reject_loop_warn():
                every = self.config.review_loop_warning_every
                if every < 1 or rejections % every != 0:
                    return
                cost = sum((r.token_report or {}).get("cost_usd") or 0
                           for r in self.runs.store.all()
                           if r.mission_pmo_id == pmo_id)
                warn = (f"⚠️ **Loop warning:** this mission has been through "
                        f"{rejections} REVIEW rejections. Cumulative recorded "
                        f"cost so far: ${cost:.2f} (runs without cost data not "
                        f"included). Add `DEVCAKE-SKIP` to stop DevCake, or "
                        f"intervene on the PR directly.")
                await self._feed(pmo_id, "issue", warn)
                if pr:
                    await self.forge.post_pr_comment(pr.number, warn)
                self._audit(pmo_id, "loop_warning", f"{rejections} rejections")

            await self._checkpoint(run, "review:reject:feed", _reject_feed)
            await self._checkpoint(run, "review:reject:pr_comment",
                                   _reject_pr_comment)
            await self._checkpoint(run, "review:reject:labels", _reject_labels)
            await self._checkpoint(run, "review:reject:loop_warn",
                                   _reject_loop_warn)
            if "review:reject" not in run.finalized_steps:
                run.finalized_steps.append("review:reject")
                self.runs.store.save(run)

    # ── decomposition finalization (docs/03 §1.3) ────────────────────────────

    async def _finalize_decomposition(self, run: Run, result: dict) -> None:
        pmo_id = run.mission_pmo_id
        live = await self.pmo.get(MissionRef(pmo_id, run.pmo_kind))
        if LABEL_CREATED in live.labels:                          # depth limit = 1
            async def _depth_limit():
                await self.pmo.swap_labels(MissionRef(pmo_id, run.pmo_kind),
                                       remove=set(), add={LABEL_SKIP})
                await self._feed(
                    pmo_id, run.pmo_kind,
                    "⛔ Depth limit: this mission was itself created by "
                    "decomposition (`DEVCAKE-CREATED`) and may not be "
                    "decomposed again. Parked with `DEVCAKE-SKIP` for a "
                    "human to re-scope.")
                self._audit(pmo_id, "depth_limit_rejected", run.run_id)
            await self._checkpoint(run, "decomp:depth_limit", _depth_limit)
            return
        drafts = result.get("decomposition") or []
        if not drafts:
            raise ValueError("decomposed outcome without decomposition list")
        for i, d in enumerate(drafts, start=1):
            deps = d.get("blocked_by") or []
            if not all(isinstance(j, int) and not isinstance(j, bool) and 1 <= j < i
                       for j in deps):
                raise ValueError(
                    f"decomposition part {i}: blocked_by must be 1-based indexes "
                    f"of EARLIER parts, got {deps!r}")
        # Redact agent-generated fields before hashing and create_mission so
        # redelivery and secrets scrubbing stay consistent (ISSUES #12).
        normalized = [{
            "title": redact(str(d.get("title") or f"part {i}")),
            "description": redact(str(d.get("description") or "")),
            "priority": str(d.get("priority") or "medium"),
            "blocked_by": list(d.get("blocked_by") or []),
        } for i, d in enumerate(drafts, start=1)]
        canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True)
        manifest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        is_project = live.pmo_kind == "project"

        existing: dict[int, str] = {}
        conflicts: list[str] = []
        for mission in await self.pmo.list_all(self.config.pmo.team_key):
            if LABEL_CREATED not in mission.labels:
                continue
            marker = DECOMPOSITION_MARKER_RE.search(mission.description or "")
            if marker and marker.group(1) == pmo_id:
                prior_manifest = marker.group(2)
                part, total = int(marker.group(3)), int(marker.group(4))
                if prior_manifest != manifest:
                    conflicts.append(
                        f"{mission.key} records a different manifest {prior_manifest[:12]}"
                    )
                    continue
                if total != len(normalized) or part not in range(1, len(normalized) + 1):
                    conflicts.append(f"{mission.key} has invalid part marker {part}/{total}")
                    continue
                if part in existing:
                    conflicts.append(f"part {part} has multiple existing missions")
                    continue
                if mission.title != normalized[part - 1]["title"]:
                    conflicts.append(f"{mission.key} title disagrees with part {part}")
                    continue
                existing[part] = mission.pmo_id

        if conflicts:
            detail = "; ".join(conflicts[:8])
            async def _decomp_conflict():
                await self.pmo.swap_labels(
                    MissionRef(pmo_id, live.pmo_kind), remove=set(),
                    add={LABEL_NEEDS_HUMAN})
                if live.status == "in_progress":
                    await self.pmo.set_status(
                        MissionRef(pmo_id, live.pmo_kind), "backlog")
                baton = (
                    "Decomposition replay conflict: no children were created. "
                    + detail
                    + ". Reconcile the existing `DEVCAKE-CREATED` missions, "
                      "then remove `DEVCAKE-NEEDS-HUMAN` to retry."
                )
                await self._feed(pmo_id, live.pmo_kind, baton)
                if live.pmo_kind == "project":
                    await self.pmo.post_feed(
                        MissionRef(pmo_id, "project"),
                        redact(baton) + "\n\n" + COMMENT_SENTINEL)
                self._audit(pmo_id, "decomposition_conflict", detail)
            await self._checkpoint(run, "decomp:conflict", _decomp_conflict)
            run.verdict = "handed off: decomposition replay conflict"
            return

        labels = {LABEL_CREATED}
        if self.config.adoption_mode == "opt_in":
            labels.add(LABEL_OPTIN)
        created = []
        child_ids: dict[int, str] = {}                            # part index → issue id

        async def _resolve_existing_child(part: int) -> str | None:
            if part in existing:
                return existing[part]
            for mission in await self.pmo.list_all(self.config.pmo.team_key):
                marker = DECOMPOSITION_MARKER_RE.search(mission.description or "")
                if marker and marker.group(1) == pmo_id \
                        and int(marker.group(3)) == part:
                    return mission.pmo_id
            return None

        for i, d in enumerate(normalized, start=1):
            title = d["title"]
            key_child = f"decomp:child:{i}"
            if i in existing:
                child_id = existing[i]
            elif key_child in run.finalized_steps:
                child_id = await _resolve_existing_child(i)
                if child_id is None:
                    raise ValueError(
                        f"decomposition part {i} checkpointed but child missing")
            else:
                footer = (f"\n\n---\n_Created by DevCake from {live.key} — "
                          f"part {i}/{len(normalized)}_\n"
                          f"`devcake:decomposition:v1 parent={pmo_id} "
                          f"manifest={manifest} part={i}/{len(normalized)}`")
                key, child_id = await self.pmo.create_mission(
                    self.config.pmo.team_key, title,
                    d["description"] + footer,
                    d["priority"], labels,
                    parent_ref=pmo_id if is_project else None)
                created.append(key)
                run.finalized_steps.append(key_child)
                self.runs.store.save(run)
            child_ids[i] = child_id
            # edges wired immediately per child (crash-safe resume; duplicate
            # relations are tolerated by the adapter) — ADR-0007
            for j in d["blocked_by"]:
                blocker_id = child_ids.get(j)
                if blocker_id:
                    rel_key = f"decomp:rel:{j}->{i}"
                    async def _rel(blocker_id=blocker_id, child_id=child_id, j=j):
                        await self.pmo.create_relation(blocker_id, child_id)
                        self._audit(child_id, "relation_created",
                                    f"blocked by part {j} ({blocker_id})")
                    await self._checkpoint(run, rel_key, _rel)
        links = ", ".join(created) or "(all already existed)"
        async def _tracking():
            if is_project:
                await self.pmo.swap_labels(MissionRef(pmo_id, "project"),
                                           remove=set(), add={LABEL_TRACKING})
                self._audit(pmo_id, "decomposed_project", links)
            else:
                await self._feed(
                    pmo_id, "issue",
                    f"🧩 Decomposed into {len(normalized)} standalone issues: "
                    f"{links}. This issue is canceled in their favor.")
                await self.pmo.set_status(MissionRef(pmo_id, "issue"), "canceled")
                self._audit(pmo_id, "decomposed_canceled", links)
        await self._checkpoint(run, "decomp:tracking", _tracking)

    # ── Relations Mapper: team-scoped MAPPER runs (ADR-0007) ─────────────────

    async def dispatch_mapper(self, dev_type: DevType, missions: list[Mission]) -> Run:
        """Dispatch a MAPPER run: a Dev whose only job is proposing missing
        blocked-by edges. No PMO writes at dispatch (no status, no labels) —
        finalize_mapper validates and applies whatever it proposes."""
        from .ids import make_run_id
        from ..prompts import MAPPER_MISSION_CAP, mapper_prompt
        eligible = [m for m in missions
                    if m.pmo_kind == "issue" and m.status not in ("done", "canceled")
                    and (self.config.adoption_mode != "opt_in"
                         or LABEL_OPTIN in m.labels)]
        if len(eligible) > MAPPER_MISSION_CAP:
            log.warning("mapper prompt truncated to %d of %d missions",
                        MAPPER_MISSION_CAP, len(eligible))
        seq = 1 + sum(1 for r in self.runs.store.all() if r.mission_type == "MAPPER")
        run_id = make_run_id("TEAM", seq, "MAPPER")

        with tracer.start_as_current_span("mission.dispatch", kind=SpanKind.PRODUCER) as span:
            span.set_attribute("devcake.run.id", run_id)
            span.set_attribute("devcake.mission.key", "TEAM")
            span.set_attribute("devcake.mission.type", "MAPPER")
            span.set_attribute("devcake.dev_type", dev_type.name)
            carrier: dict[str, str] = {}
            inject(carrier)
            traceparent = carrier.get("traceparent", "")

            redis_password = await self.messaging.create_run_user(run_id)
            spec_env = self._protocol_spec_env(
                mission_id="", mission_key="TEAM", mission_type="MAPPER",
                dev_type=dev_type, seq=seq, extra_args="")
            from .run import auth_digest
            run = Run(
                run_id=run_id, mission_key="TEAM", mission_type="MAPPER",
                pmo_ref=self.config.pmo.id, repo_ref=self.config.repo.id,
                dev_type=dev_type.name, seq=seq,
                timeout_seconds=self.config.dev_timeout_minutes * 60,
                traceparent=traceparent, auth_digest=auth_digest(redis_password),
                spec_env=spec_env,
            )
            run.spec_prompt = mapper_prompt(dev_type.identifying_prompt, eligible)
            self.runs.store.save(run)                              # durable intent first

            await self.runs.executor.start(
                params={"RUN_ID": run_id,
                        "IMAGE": HARNESSES[dev_type.harness_template].image,
                        "TRACEPARENT": traceparent,
                        "REDIS_USER": f"dev-{run_id}", "REDIS_PASSWORD": redis_password},
                dag_run_id=run_id)
            log.info("dispatched mapper %s (dev=%s, %d missions in prompt)",
                     run_id, dev_type.name, len(eligible))
            return run

    async def finalize_mapper(self, run: Run, payload: dict) -> None:
        """MAPPER runs have no host mission: no transcript/token-report comments —
        the output lands as relations + a notification comment on each blocked
        mission. Failures are logged only; the next interval simply retries."""
        result = payload.get("result") or {}
        outcome = result.get("outcome", "")
        run.token_report = redact_value(payload.get("token_report") or {})

        ctx = None
        if run.traceparent:
            from opentelemetry.propagate import extract
            ctx = extract({"traceparent": run.traceparent})
        with tracer.start_as_current_span("run.finalize", context=ctx,
                                          kind=SpanKind.CONSUMER) as span:
            span.set_attribute("devcake.run.id", run.run_id)
            span.set_attribute("devcake.outcome", outcome or "(failure artifact)")
            await self.messaging.delete_run_user(run.run_id)
            await self.messaging.delete_reply_stream(run.run_id)
            if outcome != "relations_mapped":
                run.state = "failed"
                run.error = self._dev_failure_error(run, payload)
                run.ended_at = utcnow()
                self.runs.store.save(run)
                log.warning("mapper run %s failed: %s", run.run_id, run.error)
                return
            created, rejected = await self._apply_mapper_edges(result.get("edges") or [])
            span.set_attribute("devcake.mapper.edges_created", created)
            span.set_attribute("devcake.mapper.edges_rejected", rejected)
            run.result = redact_value(result)
            run.state, run.ended_at = "finished", utcnow()
            self.runs.store.save(run)
            log.info("mapper %s finished: %d edges created, %d rejected",
                     run.run_id, created, rejected)

    async def _apply_mapper_edges(self, edges: list) -> tuple[int, int]:
        """The Dev is advisory; the app is the gatekeeper — drop edges that are
        unknown, self, terminal, duplicate, or cycle-forming (ADR-0007)."""
        missions = await self.pmo.list_all(self.config.pmo.team_key)
        by_key = {m.key.upper(): m for m in missions if m.pmo_kind == "issue"}
        graph = {m.pmo_id: set(m.blocked_by) for m in missions
                 if m.pmo_kind == "issue"}                 # node → its blockers
        created = rejected = 0
        for e in edges:
            blocker_key = str((e or {}).get("blocker") or "").strip().upper()
            blocked_key = str((e or {}).get("blocked") or "").strip().upper()
            blocker, blocked = by_key.get(blocker_key), by_key.get(blocked_key)
            reason = None
            if blocker is None or blocked is None:
                reason = "unknown mission key"
            elif blocker.pmo_id == blocked.pmo_id:
                reason = "self-edge"
            elif blocker.status in ("done", "canceled") \
                    or blocked.status in ("done", "canceled"):
                reason = "terminal mission"
            elif blocker.pmo_id in graph.get(blocked.pmo_id, set()):
                reason = "duplicate"
            elif self._creates_cycle(graph, blocker.pmo_id, blocked.pmo_id):
                reason = "would create a cycle"
            if reason:
                rejected += 1
                self._audit(blocked.pmo_id if blocked else "", "mapper_edge_rejected",
                            f"{blocker_key}→{blocked_key}: {reason}")
                log.info("mapper edge %s blocks %s rejected: %s",
                         blocker_key, blocked_key, reason)
                continue
            await self.pmo.create_relation(blocker.pmo_id, blocked.pmo_id)
            graph.setdefault(blocked.pmo_id, set()).add(blocker.pmo_id)
            self._audit(blocked.pmo_id, "relation_created",
                        f"mapper: blocked by {blocker.key}")
            await self._feed(
                blocked.pmo_id, "issue",
                f"🔗 DevCake mapped a blocking relation: this mission is blocked by "
                f"**{blocker.key}** and will not start before it finishes. Remove "
                f"the relation in the PMO if this is wrong.")
            created += 1
        return created, rejected

    @staticmethod
    def _creates_cycle(graph: dict[str, set[str]], blocker: str, blocked: str) -> bool:
        """graph maps node → its blockers. Adding `blocked ← blocker` cycles iff
        `blocked` already (transitively) blocks `blocker`."""
        stack, seen = [blocker], set()
        while stack:
            n = stack.pop()
            if n == blocked:
                return True
            if n in seen:
                continue
            seen.add(n)
            stack.extend(graph.get(n, ()))
        return False

    # ── poll-cycle sweeps (docs/04 §1) ───────────────────────────────────────

    async def sweeps(self, missions: list[Mission]) -> None:
        # prune the merge-hand-off advisories to missions still awaiting a
        # merge: covers merged/canceled AND the human label-swap intervention
        # (which also reopens the window state for a possible next episode)
        merge_ids = {m.pmo_id for m in missions
                     if m.pmo_kind == "issue" and LABEL_MERGE in m.labels
                     and m.status == "in_progress"}
        self.merge_handoffs = {k: v for k, v in self.merge_handoffs.items()
                               if k in merge_ids}
        self._merge_window_closed &= merge_ids
        # needs-human advisories: rebuilt wholesale from the label each cycle
        # (restart-safe; clears the moment the human removes the label)
        self.needs_human = {
            m.pmo_id: (f"{m.key}: needs human"
                       + (f" on {next(iter(m.labels & STAGE_LABELS))}"
                          if m.labels & STAGE_LABELS else "")
                       + (f" — {m.url}" if m.url else ""))
            for m in missions if LABEL_NEEDS_HUMAN in m.labels
        }
        # sequential by design; a per-mission await may include an adapter's
        # short transient-retry sleeps (≤ ~6 s, docs/06 §5) — expected, not a
        # hang, and non-blocking for the event loop
        for m in missions:
            try:
                # spans only when a sweep actually acts (inside the helpers)
                if m.pmo_kind == "issue" and LABEL_MERGE in m.labels \
                        and m.status == "in_progress":
                    await self._merge_sweep(m)
                if m.pmo_kind == "project" and LABEL_TRACKING in m.labels \
                        and m.status not in ("done", "canceled"):
                    await self._tracking_sweep(m)
            except Exception:
                log.exception("sweep failed for %s", m.key)

    async def _merge_sweep(self, m: Mission) -> None:
        pr = await self.forge.get_pr_by_branch(mission_branch(m.key))
        if not pr:
            return
        state = await self.forge.pr_state(pr.number)
        if state.merged or state.state == "closed":
            with tracer.start_as_current_span("sweep.merge") as span:
                span.set_attribute("devcake.mission.key", m.key)
                span.set_attribute("devcake.outcome",
                                   "merged" if state.merged else "closed")
                await self.pmo.swap_labels(m.ref, remove={LABEL_MERGE}, add=set())
                if state.merged:
                    await self.pmo.set_status(m.ref, "done")
                    await self._feed(
                        m.pmo_id, "issue",
                        f"✅ PR {state.url} merged — mission done (merge sweep).")
                    self._audit(m.pmo_id, "merge_sweep_done", state.url)
                else:
                    await self.pmo.set_status(m.ref, "canceled")
                    await self._feed(
                        m.pmo_id, "issue",
                        f"🚫 PR {state.url} was closed without merging — mission "
                        f"canceled (merge sweep).")
                    self._audit(m.pmo_id, "merge_sweep_canceled", state.url)
        else:
            # advisory banner (docs/11): an open PR on DEVCAKE-MERGE awaits a
            # human — unless the deferred-retry window is actively running
            # (_deferred_merge_retry pops the entry while it drives the window)
            self.merge_handoffs[m.pmo_id] = (
                f"{m.key}: awaiting human merge — {state.url}")
            if self.config.auto_merge:
                await self._deferred_merge_retry(m, pr, state.url)

    async def _deferred_merge_retry(self, m: Mission, pr,
                                    pr_url: str) -> None:
        """docs/03 §4.1 deferred-merge window: while `devcake:merge-retry` is
        the latest merge-state marker in the feed, keep watching the PR each
        sweep cycle — merge when it becomes ready, route to EXECUTE if a
        conflict emerges, and hand off to a human once
        merge_retry_window_minutes elapse. Elapsed time is measured from the
        marker entry's PMO timestamp (no local clocks), so the window is
        live-tunable and restart-safe. The label stays DEVCAKE-MERGE
        throughout: a manual human merge mid-window is caught by the
        external-merge branch above on the next cycle."""
        if m.pmo_id in self._merge_window_closed:
            return  # window known closed — skip the per-cycle feed read
        act = await self.pmo.get_activity(m.ref)
        retry_ts = handoff_ts = None
        for e in act.entries:
            body = self._unquoted(e.body)
            ts = self._aware(e.ts)  # a naive PMO timestamp must not TypeError
            if MERGE_RETRY_MARKER in body:
                retry_ts = max(retry_ts, ts) if retry_ts else ts
            if MERGE_HANDOFF_MARKER in body:
                handoff_ts = max(handoff_ts, ts) if handoff_ts else ts
        if not retry_ts or (handoff_ts and handoff_ts >= retry_ts):
            self._merge_window_closed.add(m.pmo_id)
            return  # no active retry window (auto_merge-OFF parks land here)
        window = self.config.merge_retry_window_minutes
        if (utcnow() - retry_ts).total_seconds() / 60 > window:
            with tracer.start_as_current_span("sweep.merge_retry") as span:
                span.set_attribute("devcake.mission.key", m.key)
                span.set_attribute("devcake.outcome", "window_exhausted")
                span.set_status(Status(StatusCode.ERROR,
                                       f"unmergeable after {window} min"))
                await self._feed(
                    m.pmo_id, "issue",
                    f"⚠️ Still unmergeable after {window} min — awaiting human "
                    f"merge of {pr_url} (`DEVCAKE-MERGE`). {MERGE_HANDOFF_MARKER}")
                self._audit(m.pmo_id, "merge_retry_exhausted", pr_url)
                self._merge_window_closed.add(m.pmo_id)
            return
        # window ACTIVE: DevCake is still driving the merge — no human action
        # needed, so the sweep's banner entry comes back off
        self.merge_handoffs.pop(m.pmo_id, None)
        verdict = await self.forge.mergeable(pr.number)
        if verdict is None:
            return  # still computing / CI running — next cycle re-reads
        # a False verdict can be a non-blocking "behind" (strict up-to-date
        # rules are what make it fail) — one plain merge attempt is far
        # cheaper than an EXECUTE rework, so always try the merge first and
        # only route to rework when it actually fails on a real conflict
        with tracer.start_as_current_span("sweep.merge_retry") as span:
            span.set_attribute("devcake.mission.key", m.key)
            span.set_attribute("devcake.merge.verdict", str(verdict))
            try:
                await self.forge.merge(pr.number)
            except Exception:
                if verdict is False:
                    span.set_attribute("devcake.outcome", "conflict")
                    if not await self._maybe_route_conflict_to_execute(
                            m.pmo_id, m.key, pr_url, LABEL_MERGE):
                        span.set_attribute("devcake.outcome", "conflict_handoff")
                        await self._feed(
                            m.pmo_id, "issue",
                            f"⚠️ Merge conflict on {pr_url} and auto-resolve is "
                            f"unavailable (toggle off or attempts exhausted) — "
                            f"awaiting human merge (`DEVCAKE-MERGE`). "
                            f"{MERGE_HANDOFF_MARKER}")
                        self._audit(m.pmo_id, "merge_retry_exhausted", pr_url)
                        self._merge_window_closed.add(m.pmo_id)
                        self.merge_handoffs[m.pmo_id] = (
                            f"{m.key}: awaiting human merge — {pr_url}")
                else:
                    # state may have moved under us; next cycle re-reads
                    span.set_attribute("devcake.outcome", "merge_failed_transient")
                    log.debug("deferred merge retry failed for %s", m.key,
                              exc_info=True)
                return
            span.set_attribute("devcake.outcome", "merged")
            await self.pmo.swap_labels(m.ref, remove={LABEL_MERGE}, add=set())
            await self.pmo.set_status(m.ref, "done")
            await self._feed(
                m.pmo_id, "issue",
                f"✅ Merged after deferred retry ({pr_url}). Mission done.")
            self._audit(m.pmo_id, "merge_retry_succeeded", pr_url)

    async def _tracking_sweep(self, m: Mission) -> None:
        children = await self.pmo.children_of(m.ref)
        if children and all(c.status in ("done", "canceled") for c in children):
            with tracer.start_as_current_span("sweep.tracking") as span:
                span.set_attribute("devcake.mission.key", m.key)
                span.set_attribute("devcake.children", len(children))
                await self.pmo.set_status(m.ref, "done")
                await self.pmo.swap_labels(m.ref, remove={LABEL_TRACKING}, add=set())
                self._audit(m.pmo_id, "tracking_sweep_completed",
                            f"{len(children)} children")
            log.info("project %s auto-completed (%d children done)", m.key, len(children))

    async def restore_after_failure(self, run: Run) -> None:
        """Revert the dispatch-time backlog→in_progress write after a failed attempt,
        iff the mission is still exactly as we left it (live re-read; human edits win)."""
        if run.stage_label_at_dispatch is not None or not run.mission_pmo_id:
            return  # only ONBOARD dispatches from backlog change the status
        try:
            live = await self.pmo.get(MissionRef(run.mission_pmo_id, run.pmo_kind))
            if live.status == "in_progress" and self._stage_of(live) is None:
                await self.pmo.set_status(
                    MissionRef(run.mission_pmo_id, run.pmo_kind), "backlog")
                self._audit(run.mission_pmo_id, "set_status",
                            "backlog (restored after failed attempt)")
        except Exception:
            log.exception("status restore failed for %s", run.run_id)

    async def _post_transcript(self, run: Run, transcript: str) -> None:
        transcript = redact(transcript)
        name = f"{run.seq}_{run.mission_type}.md"
        body = f"🧾 DevCake transcript `{name}` (run `{run.run_id}`)\n\n---\n\n{transcript}"
        if run.pmo_kind == "project":
            await self._feed(run.mission_pmo_id, "project", body)
            return
        try:  # docs/05 §4: transcripts always live as attachments, never inline
            url = await self.pmo.upload_attachment(run.mission_pmo_id, name,
                                                   transcript.encode())
            # the backticked `{name}` must stay in the comment — STEP_MARKER
            # counts it for seq derivation (docs/02 §8)
            body = (f"🧾 DevCake transcript `{name}` (run `{run.run_id}`) — "
                    f"attached: [{name}]({url})")
        except Exception:  # INV-5: the transcript is always posted, even inline
            log.exception("transcript upload failed — posting inline")
        await self._feed(run.mission_pmo_id, "issue", body)
        self._audit(run.mission_pmo_id, "transcript", name)

    @staticmethod
    def _token_report_md(run: Run, tr: dict) -> str:
        def fmt(v):  # noqa: ANN001
            return "—" if v is None else v
        cost = tr.get("cost_usd")
        return (
            f"🧮 DevCake token report — step {run.seq} ({run.mission_type}, {run.dev_type})\n"
            f"model: {fmt(tr.get('model'))} · input: {fmt(tr.get('input_tokens'))} · "
            f"output: {fmt(tr.get('output_tokens'))}\n"
            f"cache read/write: {fmt(tr.get('cache_read_tokens'))}/"
            f"{fmt(tr.get('cache_write_tokens'))}"
            + (f" · total: {tr['total_tokens']}" if tr.get('total_tokens') is not None else "")
            + (f"\ncost: ${cost:.4f}" if cost is not None else "")
            + f"\nextraction: {fmt(tr.get('extraction_method'))}\nrun: {run.run_id}")

    # ── activity rendering for the Dev workspace (docs/07 §2 index format) ──

    async def activity_payload(self, pmo_id: str, kind: str = "issue") -> dict:
        if kind == "project":
            # projects have no comments/attachments: ACTIVITY.md = the brief itself
            m = await self.pmo.get(MissionRef(pmo_id, "project"))
            md = "\n".join([
                f"# {m.key}: {m.title}",
                f"> Kind: project · Status: {m.status} · Priority: {m.priority} · URL: {m.url}",
                f"> Labels: {', '.join(sorted(m.labels)) or '(none)'}", "",
                "## Description", m.description or "(none)", "",
                "## Activity", "(projects carry no comment feed — see child issues)"])
            return {"activity_md": md, "attachments": []}
        act = await self.pmo.get_activity(MissionRef(pmo_id, "issue"))
        m = act.mission
        lines = [
            f"# {m.key}: {m.title}",
            f"> Kind: {m.pmo_kind} · Status: {m.status} · Priority: {m.priority} · URL: {m.url}",
            f"> Labels: {', '.join(sorted(m.labels)) or '(none)'}", "",
            "## Description", m.description or "(none)", "",
            "## Activity (chronological index — long bodies live as files in this folder)",
            "Entries marked 🧑 HUMAN are instructions/steering from a person — they",
            "are authoritative. Entries marked 🤖 DevCake are DevCake's own records.",
        ]
        attachments = []
        used: set[str] = {"ACTIVITY.md"}     # docs/07 §2: suffix-dedupe filenames
        for e in act.entries:
            body = e.body or ""
            # provenance is sentinel-based, never author-based (docs/03 §8a):
            # DevCake may post with the operator's own PMO credentials
            provenance = "🤖 DevCake" if self._is_devcake_comment(body) else "🧑 HUMAN"
            if len(body) > FEED_INLINE_MAX:                 # externalize long bodies
                fname = self._unique_name(f"entry-{e.ts:%Y%m%dT%H%M%S}.md", used)
                attachments.append({"filename": fname,
                                    "content_b64": base64.b64encode(body.encode()).decode()})
                body = body[:300].replace("\n", " ") + f"… — see: {fname}"
            lines.append(f"### {e.ts:%Y-%m-%d %H:%M} — {e.author} — {provenance} ({e.kind})")
            lines.append(body)
            # the adapter resolves human-readable names (AttachmentRef.name) —
            # the domain never parses vendor asset URLs
            for att in e.attachments:
                try:
                    data = await self.pmo.download_asset(att.url)
                    fname = self._unique_name(
                        att.name or att.url.rsplit("/", 1)[-1][:80] or "attachment.bin",
                        used)
                    attachments.append({"filename": fname,
                                        "content_b64": base64.b64encode(data).decode()})
                    lines.append(f"[attachment: {fname}]")
                except Exception:
                    lines.append(f"[attachment unavailable: {att.url}]")
            lines.append("")
        return {"activity_md": "\n".join(lines), "attachments": attachments}


# ── Relations Mapper (re-export; implementation in mapper_service.py) ───────
from .mapper_service import MapperBusy, MapperService, MapperUnconfigured  # noqa: E402
