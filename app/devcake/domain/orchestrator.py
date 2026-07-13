"""Mission scheduling and finalization (docs/04) — M3 scope: ONBOARD on Issues.

PLAN/EXECUTE/REVIEW dispatch and the trivial/decompose finalizations land at
M4/M5; the mechanics here (ordering, caps, grace cycle, compare-and-transition,
attempt counting) are the permanent ones.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.trace import SpanKind, Status, StatusCode

from ..config import AppConfig, DevType
from ..harness import HARNESSES
from ..security import redact
from ..telemetry import OO_ORG, OO_URL
from .model import (LABEL_CREATED, LABEL_EXECUTE, LABEL_FAILED, LABEL_MERGE,
                    LABEL_NEEDS_HUMAN, LABEL_OPTIN, LABEL_PLAN, LABEL_REVIEW,
                    LABEL_SKIP, LABEL_TRACKING, Mission, MissionType, PRIORITY_RANK,
                    STAGE_LABELS, derive, find_cycles)
from .run import Run, utcnow
from .runs import RunManager

if TYPE_CHECKING:  # typing only — the domain never imports adapters at runtime
    from ..adapters.linear import LinearAdapter
    from ..adapters.redis import Messaging

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")

# M5: the full state machine is dispatchable, projects included (ADR-0006).
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

AUDIT_PATH = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "state" / "events.jsonl"


def _oo_basic_auth() -> str:
    email = os.environ.get("OO_ROOT_EMAIL", "")
    password = os.environ.get("OO_ROOT_PASSWORD", "")
    return base64.b64encode(f"{email}:{password}".encode()).decode()


class MissionManager:
    def __init__(self, config: AppConfig, dev_types: dict[str, DevType],
                 pmo: LinearAdapter, forge, runs: RunManager,
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
        self.blocked_reasons: dict[str, str] = {}  # last gate_map (advisory mirror)
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

    def rotate_grace(self) -> None:
        self._grace, self._grace_next = self._grace_next, set()

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
                                  f"unblock; delete one relation in Linear")
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
            if dev_type is None or dev_type.name in self.breakers:
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
                        memo[bid] = await self.pmo.get_mission(bid)
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
        live = await self._live(mission.pmo_id, mission.pmo_kind)  # live re-read
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
        else:
            activity = await self.pmo.get_activity(mission.pmo_id)
            seq = self._derive_seq(activity)
        # attempts restart when a human removes DEVCAKE-FAILED (docs/15 §3):
        # only failures newer than the last give-up event count
        since = self._last_giveup_ts(mission.pmo_id)
        attempt = 1 + sum(1 for r in self.runs.store.all()
                          if r.mission_pmo_id == mission.pmo_id
                          and r.mission_type == mtype.value and r.seq == seq
                          and r.state in ("failed", "timed_out", "orphaned")
                          and "DEV_AUTH" not in (r.error or "")
                          and (since is None or r.created_at.isoformat() > since))
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
                    forge=self.config.repo.forge),
                MissionType.REVIEW: lambda: review_prompt(dev_type.identifying_prompt, live),
            }[mtype]()

            spec_env = {
                "DEVCAKE_MISSION_ID": mission.pmo_id,
                "DEVCAKE_MISSION_KEY": mission.key,
                "DEVCAKE_MISSION_TYPE": mtype.value,
                "DEVCAKE_DEV_TYPE": dev_type.name,
                "DEVCAKE_HARNESS": dev_type.harness_template,  # app-authoritative
                "DEVCAKE_SEQ": str(seq),
                "DEVCAKE_REPO_URL": self.config.repo.url,
                "DEVCAKE_DEFAULT_BRANCH": "main",
                "DEVCAKE_FORGE": self.config.repo.forge,
                "DEVCAKE_FORGE_TOKEN": self.config.repo.token,
                "DEVCAKE_EXTRA_ARGS": assignment.extra_cli_args,
                "DEVCAKE_MODEL": dev_type.model,
                "OTEL_EXPORTER_OTLP_ENDPOINT": f"{OO_URL}/api/{OO_ORG}/v1/traces",
                "OTEL_EXPORTER_OTLP_BASIC": _oo_basic_auth(),
            }
            env_creds, spec_files = self._credential_spec(dev_type)
            spec_env.update(env_creds)
            run = Run(
                run_id=run_id, mission_key=mission.key, mission_type=mtype.value,
                pmo_kind=mission.pmo_kind,
                pmo_ref=self.config.pmo.id, repo_ref=self.config.repo.id,
                dev_type=dev_type.name, seq=seq, attempt_of_step=attempt,
                timeout_seconds=self.config.dev_timeout_minutes * 60,
                traceparent=traceparent, redis_password=redis_password,
                spec_env=spec_env, spec_files=spec_files,
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
                await self._set_status(mission.pmo_id, mission.pmo_kind, "in_progress")
                self._audit(mission.pmo_id, "set_status", "in_progress")
            log.info("dispatched %s (attempt %d, dev=%s)", run_id, attempt, dev_type.name)
            return run

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
                            "or upload it on the admin Config tab", p, dev_type.name)
        return env, files

    async def _live(self, pmo_id: str, kind: str) -> Mission:
        return await (self.pmo.get_project(pmo_id) if kind == "project"
                      else self.pmo.get_mission(pmo_id))

    async def _swap(self, pmo_id: str, kind: str, remove: set, add: set) -> None:
        if kind == "project":
            await self.pmo.swap_labels_project(pmo_id, remove, add)
        else:
            await self.pmo.swap_labels(pmo_id, remove, add)

    async def _set_status(self, pmo_id: str, kind: str, status: str) -> None:
        if kind == "project":
            await self.pmo.set_project_status(pmo_id, status)
        else:
            await self.pmo.set_status(pmo_id, status)

    async def _feed(self, pmo_id: str, kind: str, markdown: str) -> None:
        """The single choke-point for PMO comments: redaction + the provenance
        sentinel. Bodies over FEED_INLINE_MAX are uploaded as .md attachments
        and replaced by a short referencing comment (docs/05 §4); the sentinel
        goes on the comment, never inside the attachment, so provenance
        classification keeps working. Upload failures fall back to posting
        inline — an upload outage must never lose feed content. Projects have
        no issue-style comments API (verified at M2/M5): their run artifacts
        live in the audit log + OpenObserve; the substance lands on the child
        issues anyway (ADR-0006)."""
        markdown = redact(markdown, [r.redis_password for r in self.runs.store.active()
                                     if r.redis_password])
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
        await self.pmo.post_comment(
            pmo_id, markdown.rstrip() + "\n\n" + COMMENT_SENTINEL)

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
    def _last_giveup_ts(pmo_id: str) -> str | None:
        try:
            ts = None
            with open(AUDIT_PATH) as f:
                for line in f:
                    e = json.loads(line)
                    if e.get("pmo_id") == pmo_id and e.get("action") == "devcake_failed":
                        ts = e["ts"]
            return ts
        except FileNotFoundError:
            return None

    async def _give_up(self, mission: Mission, mtype: MissionType, attempts: int) -> None:
        if LABEL_FAILED in mission.labels:
            return
        with tracer.start_as_current_span("mission.give_up") as span:
            span.set_attribute("devcake.mission.key", mission.key)
            span.set_attribute("devcake.mission.type", mtype.value)
            span.set_attribute("devcake.run.attempt", attempts)
        await self._swap(mission.pmo_id, mission.pmo_kind, remove=set(), add={LABEL_FAILED})
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

            run.token_report = token_report      # persisted: cumulative-cost source
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
                if exit_code == 12:  # DEV_AUTH: breaker, never counts as an attempt (docs/15 §4)
                    self.breakers[run.dev_type] = f"auth failure in {run.run_id}"
                    run.state = "failed"
                    run.error = "DEV_AUTH (does not count toward attempts; breaker tripped)"
                else:
                    run.state = "failed"
                    run.error = f"dev failure artifact (exit {exit_code})"
                run.ended_at = utcnow()
                run.redis_password = None
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
                    run.result = result
                    run.state = "failed"
                    run.error = f"DEV_BAD_OUTPUT: {e}"
                    run.ended_at = utcnow()
                    run.redis_password = None
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
            run.result = result
            run.state, run.ended_at = "finished", utcnow()
            run.redis_password = None
            self.runs.store.save(run)
            # app-level judgment onto the trace: Dagu can report the step green
            # while _transition refused to act — make that visible in OO
            span.set_attribute("devcake.verdict", run.verdict or "success")
            if run.verdict and not run.verdict.startswith("handed off"):
                span.set_status(Status(StatusCode.ERROR, run.verdict))
                span.add_event("devcake.verdict", {"detail": run.verdict})
            log.info("finalized %s (%s)", run.run_id, outcome)

    async def _transition(self, run: Run, result: dict, plan_md: str | None) -> None:
        outcome = result.get("outcome", "")
        pmo_id = run.mission_pmo_id
        if outcome not in LEGAL_OUTCOMES.get(run.mission_type, frozenset()):
            # illegal (or unknown) outcome for this step — the trust boundary.
            # Park for a human; never act on it (docs/03 §6, docs/15).
            await self._swap(pmo_id, run.pmo_kind, remove=set(), add={LABEL_SKIP})
            await self._feed(
                pmo_id, run.pmo_kind,
                f"⛔ DevCake received outcome `{outcome or '(empty)'}` from a "
                f"**{run.mission_type}** run — not a legal outcome for that step. "
                f"No transition was applied; parked with `DEVCAKE-SKIP` for a "
                f"human to inspect.")
            self._audit(pmo_id, "illegal_outcome",
                        f"{outcome or '(empty)'} from {run.mission_type}")
            run.verdict = (f"rejected: outcome {outcome or '(empty)'} is illegal "
                           f"for {run.mission_type} — parked with DEVCAKE-SKIP")
            log.warning("illegal outcome %r from %s run %s — parked",
                        outcome, run.mission_type, run.run_id)
            return
        live = await self._live(pmo_id, run.pmo_kind)               # live re-read
        if run.pmo_kind == "project" and outcome not in ("decomposed", "human_needed"):
            await self._swap(pmo_id, "project", remove=set(), add={LABEL_SKIP})
            self._audit(pmo_id, "project_bad_outcome_parked", outcome)
            run.verdict = (f"rejected: project returned {outcome} (only decomposed "
                           f"is legal) — parked with DEVCAKE-SKIP")
            log.warning("project %s returned %s (only decomposed is legal) — parked",
                        run.mission_key, outcome)
            return
        if self._stage_of(live) != run.stage_label_at_dispatch:
            await self._feed(
                pmo_id, run.pmo_kind,
                f"ℹ️ DevCake completed a **{run.mission_type}** run (`{run.run_id}`), but "
                f"this mission's state was changed externally while it ran. The output is "
                f"posted above; **no status or label changes were applied**.")
            self._audit(pmo_id, "external_transition", run.run_id)
            run.verdict = ("skipped: mission state changed externally while the "
                           "run was in flight — no transition applied")
            log.warning("EXTERNAL_TRANSITION on %s — no labels applied", run.run_id)
            return

        if outcome in ("executed", "executed_trivially", "reviewed"):
            await self._flag_out_of_pipeline_merge(run)

        if outcome == "planned":
            url = await self.pmo.upload_attachment(pmo_id, f"PLAN_{run.seq}.md",
                                                   redact(plan_md or "").encode())
            await self._feed(
                pmo_id, run.pmo_kind,
                f"📋 DevCake plan for this mission: [PLAN_{run.seq}.md]({url})")
            await self.pmo.swap_labels(pmo_id, remove={LABEL_PLAN}, add={LABEL_EXECUTE})
            self._audit(pmo_id, "label_swap", f"{LABEL_PLAN}→{LABEL_EXECUTE}")
        elif outcome == "executed":
            from .model import LABEL_REVIEW
            await self.pmo.swap_labels(pmo_id, remove={LABEL_EXECUTE}, add={LABEL_REVIEW})
            await self._feed(
                pmo_id, run.pmo_kind,
                f"🔀 DevCake opened/updated the pull request: "
                f"{result.get('pr_url', '(no url reported)')} — awaiting REVIEW.")
            self._audit(pmo_id, "label_swap", f"{LABEL_EXECUTE}→{LABEL_REVIEW}")
        elif outcome == "executed_trivially":
            from .model import LABEL_REVIEW
            await self.pmo.swap_labels(pmo_id, remove=set(), add={LABEL_REVIEW})
            await self._feed(
                pmo_id, run.pmo_kind,
                f"🔀 Trivial path: PR opened ({result.get('pr_url', '?')}) — "
                f"the trivial path never skips REVIEW (docs/03 §1.1).")
            self._audit(pmo_id, "label_add", LABEL_REVIEW)
        elif outcome == "reviewed":
            await self._finalize_review(run, result)
        elif outcome == "decomposed":
            await self._finalize_decomposition(run, result)
        elif outcome == "plan_needed" and plan_md:
            url = await self.pmo.upload_attachment(pmo_id, f"PLAN_{run.seq}.md",
                                                   redact(plan_md).encode())
            await self._feed(
                pmo_id, run.pmo_kind,
                f"📋 DevCake attached an opportunistic plan from triage: "
                f"[PLAN_{run.seq}.md]({url}) — skipping the PLAN step.")
            await self.pmo.swap_labels(pmo_id, remove=set(), add={LABEL_EXECUTE})
            self._audit(pmo_id, "label_add", LABEL_EXECUTE)
        elif outcome == "plan_needed":
            await self.pmo.swap_labels(pmo_id, remove=set(), add={LABEL_PLAN})
            self._audit(pmo_id, "label_add", LABEL_PLAN)
        elif outcome == "human_needed":
            # deliberate hand-off (docs/03 §4a, ADR-0007): the run finished
            # cleanly, so it never counts toward max_attempts; the stage label
            # stays so work resumes at the same step once the human removes
            # DEVCAKE-NEEDS-HUMAN. Repeats on the same step escalate a warning —
            # never an auto-park; the human always decides (founder decision).
            await self._swap(pmo_id, run.pmo_kind, remove=set(), add={LABEL_NEEDS_HUMAN})
            if run.stage_label_at_dispatch is None and live.status == "in_progress":
                # ONBOARD case: without this, removing the label later lands on
                # derivation row 9 (in_progress, no stage label) and strands
                await self._set_status(pmo_id, run.pmo_kind, "backlog")
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
                # _feed suppresses project comments (no comments API) — but a
                # baton pass MUST be PMO-visible, so it goes out as a project
                # update, Linear's project-native feed (docs/05 §6)
                try:
                    await self.pmo.create_project_update(
                        pmo_id, redact(baton) + "\n\n" + COMMENT_SENTINEL)
                except Exception:
                    log.warning("project-update hand-off failed for %s — "
                                "summary lives in the audit log only",
                                run.mission_key, exc_info=True)
            self._audit(pmo_id, "devcake_needs_human",
                        f"#{nth}: " + (result.get("summary") or "")[:110])
            run.verdict = f"handed off: needs human on {run.mission_type}"
            # advisory appears immediately (sweeps() keeps it in sync after)
            self.needs_human[pmo_id] = (
                f"{run.mission_key}: needs human on {run.mission_type}"
                + (f" — {live.url}" if live.url else ""))
        else:
            await self.pmo.swap_labels(pmo_id, remove=set(), add={LABEL_SKIP})
            await self._feed(
                pmo_id, run.pmo_kind,
                f"ℹ️ DevCake received unknown outcome `{outcome}` — parked with "
                f"`DEVCAKE-SKIP` for a human to inspect.")
            self._audit(pmo_id, "label_add", f"{LABEL_SKIP} (unknown outcome {outcome})")
            run.verdict = (f"rejected: unknown outcome {outcome} — parked with "
                           f"DEVCAKE-SKIP")

    async def _flag_out_of_pipeline_merge(self, run: Run) -> None:
        """Detection tripwire (docs/14, ADR-0007 addendum): the Dev's forge token
        can merge unless branch protection forbids it. If the mission's PR turns
        up merged while the mission is still mid-pipeline, say so loudly —
        detection only; a human decides (they may have merged early themselves)."""
        try:
            pr = await self.forge.get_pr_by_branch(f"devcake/{run.mission_key}")
            if not pr:
                return
            state = await self.forge.pr_state(pr["number"])
            if not state["merged"]:
                return
            await self._feed(
                run.mission_pmo_id, run.pmo_kind,
                f"⚠️ **Out-of-pipeline merge detected:** {state['url']} is already "
                f"merged, but this mission is still mid-pipeline "
                f"({run.mission_type}). If you merged it yourself on purpose, "
                f"mark the mission Done (or add `DEVCAKE-SKIP`); otherwise check "
                f"who merged it — DevCake did not.")
            self._audit(run.mission_pmo_id, "out_of_pipeline_merge", state["url"])
            self.anomalies[run.mission_pmo_id] = (
                f"{run.mission_key}: PR merged outside the pipeline ({state['url']})")
            log.warning("out-of-pipeline merge on %s (%s)", run.mission_key,
                        state["url"])
        except Exception:
            log.debug("out-of-pipeline merge check failed for %s",
                      run.mission_key, exc_info=True)

    # ── REVIEW finalization (docs/03 §4, merge-before-Done) ──────────────────

    async def _conflict_attempts(self, pmo_id: str) -> int:
        """Prior auto-resolve attempts, derived from feed markers — the PMO is
        the source of truth (docs/03), so the count survives restarts and a
        human deleting directive comments deliberately resets it."""
        act = await self.pmo.get_activity(pmo_id)
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
                f"Next Dev: sync `devcake/{key}` with the default branch, "
                f"resolve the conflicts, and push; the PR then returns to "
                f"REVIEW. `devcake:conflict-resolve:{n + 1}`")
            await self.pmo.swap_labels(pmo_id, remove={from_label},
                                       add={LABEL_EXECUTE})
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
        pr = await self.forge.get_pr_by_branch(f"devcake/{run.mission_key}")
        pr_url = (pr or {}).get("html_url") or result.get("pr_url") or "?"
        footer = self.forge.approval_footer(pr_url)

        if verdict == "approve":
            formal = False
            if pr:
                await self.forge.post_pr_comment(
                    pr["number"],
                    "## DevCake REVIEW: APPROVED-BY-DEVCAKE ✅\n\n" + report + footer)
                try:
                    formal = await self.forge.approve(pr["number"])
                except Exception:
                    log.exception("formal approval failed — falling back to marker")
            if self.config.auto_merge and pr:
                try:
                    await self.forge.merge(pr["number"])       # merge BEFORE Done
                    await self.pmo.swap_labels(pmo_id, remove={LABEL_REVIEW}, add=set())
                    await self.pmo.set_status(pmo_id, "done")
                    await self._feed(
                        pmo_id, "issue",
                        f"✅ REVIEW approved; PR merged ({pr_url}). Mission done.")
                    self._audit(pmo_id, "review_approve_merged", pr_url)
                except Exception as e:
                    # docs/03 §4.1 — three-way failure branch. A detection
                    # failure means mstate stays None → deferred retry, which
                    # degrades safely to the hand-off when the window expires.
                    mstate = None
                    try:
                        mstate = await self.forge.mergeable(pr["number"])
                    except Exception:
                        log.exception("mergeable check failed for %s", pr_url)
                    if mstate is False and await self._maybe_route_conflict_to_execute(
                            pmo_id, run.mission_key, pr_url, LABEL_REVIEW):
                        return
                    await self.pmo.swap_labels(pmo_id, remove={LABEL_REVIEW},
                                               add={LABEL_MERGE})
                    if mstate is not False and \
                            self.config.merge_retry_window_minutes > 0:
                        await self._feed(
                            pmo_id, "issue",
                            f"⏳ REVIEW approved but the merge is not possible "
                            f"yet ({e}) — DevCake keeps retrying for up to "
                            f"{self.config.merge_retry_window_minutes} minutes "
                            f"(mergeability computing / CI pipeline running). "
                            f"You can merge {pr_url} manually at any time. "
                            f"{MERGE_RETRY_MARKER}")
                        self._audit(pmo_id, "merge_deferred", str(e)[:120])
                        # a fresh retry marker opens a new episode — the sweep
                        # must read the feed again
                        self._merge_window_closed.discard(pmo_id)
                    else:
                        await self._feed(
                            pmo_id, "issue",
                            f"⚠️ REVIEW approved but auto-merge failed ({e}); "
                            f"awaiting human merge of {pr_url} "
                            f"(`DEVCAKE-MERGE`). {MERGE_HANDOFF_MARKER}")
                        self._audit(pmo_id, "review_approve_merge_failed",
                                    str(e)[:120])
            else:
                await self.pmo.swap_labels(pmo_id, remove={LABEL_REVIEW},
                                           add={LABEL_MERGE})
                await self._feed(
                    pmo_id, "issue",
                    f"✅ REVIEW approved "
                    f"({'formal approval filed' if formal else 'APPROVED-BY-DEVCAKE marker'}). "
                    f"Awaiting human merge of {pr_url} — the merge sweep completes "
                    f"this mission once it merges." + footer)
                self._audit(pmo_id, "review_approve_awaiting_merge", pr_url)
        else:  # reject
            rejections = 1 + sum(
                1 for r in self.runs.store.all()
                if r.mission_pmo_id == pmo_id and r.mission_type == "REVIEW"
                and r.state == "finished" and (r.result or {}).get("verdict") == "reject")
            feed_body = (f"🔁 REVIEW rejected (round {rejections}) — back to "
                         f"EXECUTE.\n\n" + report)
            if report:
                try:  # docs/05 §4: full report as attachment, short feed comment
                    # redact with THIS run's relay password too — the attachment
                    # bypasses _feed's active-run redaction
                    name = f"{run.seq}_REVIEW_REPORT.md"
                    url = await self.pmo.upload_attachment(
                        pmo_id, name,
                        redact(report, [run.redis_password or ""]).encode())
                    feed_body = (f"🔁 REVIEW rejected (round {rejections}) — back "
                                 f"to EXECUTE. Full report attached: "
                                 f"[{name}]({url})")
                except Exception:
                    log.exception("review report upload failed — posting inline")
            await self._feed(pmo_id, "issue", feed_body)
            if pr:
                await self.forge.post_pr_comment(
                    pr["number"],
                    "## DevCake REVIEW: changes requested 🔁\n\n" + report + footer)
            await self.pmo.swap_labels(pmo_id, remove={LABEL_REVIEW}, add={LABEL_EXECUTE})
            self._audit(pmo_id, "label_swap", f"{LABEL_REVIEW}→{LABEL_EXECUTE}")
            every = self.config.review_loop_warning_every
            if rejections % every == 0:
                cost = sum((r.token_report or {}).get("cost_usd") or 0
                           for r in self.runs.store.all() if r.mission_pmo_id == pmo_id)
                warn = (f"⚠️ **Loop warning:** this mission has been through {rejections} "
                        f"REVIEW rejections. Cumulative recorded cost so far: ${cost:.2f} "
                        f"(runs without cost data not included). Add `DEVCAKE-SKIP` to "
                        f"stop DevCake, or intervene on the PR directly.")
                await self._feed(pmo_id, "issue", warn)
                if pr:
                    await self.forge.post_pr_comment(pr["number"], warn)
                self._audit(pmo_id, "loop_warning", f"{rejections} rejections")

    # ── decomposition finalization (docs/03 §1.3) ────────────────────────────

    async def _finalize_decomposition(self, run: Run, result: dict) -> None:
        pmo_id = run.mission_pmo_id
        live = await self._live(pmo_id, run.pmo_kind)
        if LABEL_CREATED in live.labels:                          # depth limit = 1
            await self._swap(pmo_id, run.pmo_kind, remove=set(), add={LABEL_SKIP})
            await self._feed(
                pmo_id, run.pmo_kind,
                "⛔ Depth limit: this mission was itself created by decomposition "
                        "(`DEVCAKE-CREATED`) and may not be decomposed again. Parked with "
                        "`DEVCAKE-SKIP` for a human to re-scope.")
            self._audit(pmo_id, "depth_limit_rejected", run.run_id)
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
        is_project = live.pmo_kind == "project"
        existing: dict[str, str] = {}                             # title → issue id
        if is_project:
            existing = {m.title: m.pmo_id
                        for m in await self.pmo.children_of_project(pmo_id)}
        labels = {LABEL_CREATED}
        if self.config.adoption_mode == "opt_in":
            labels.add(LABEL_OPTIN)
        created = []
        child_ids: dict[int, str] = {}                            # part index → issue id
        for i, d in enumerate(drafts, start=1):
            title = d.get("title", f"part {i}")
            if title in existing:                                 # idempotent top-up
                child_id = existing[title]
            else:
                footer = (f"\n\n---\n_Created by DevCake from {live.key} — "
                          f"part {i}/{len(drafts)}_")
                key, child_id = await self.pmo.create_mission(
                    self.config.pmo.team_key, title,
                    (d.get("description") or "") + footer,
                    d.get("priority") or "medium", labels,
                    project_id=pmo_id if is_project else None)
                created.append(key)
            child_ids[i] = child_id
            # edges wired immediately per child (crash-safe resume; duplicate
            # relations are tolerated by the adapter) — ADR-0007
            for j in d.get("blocked_by") or []:
                blocker_id = child_ids.get(j)
                if blocker_id:
                    await self.pmo.create_relation(blocker_id, child_id)
                    self._audit(child_id, "relation_created",
                                f"blocked by part {j} ({blocker_id})")
        links = ", ".join(created) or "(all already existed)"
        if is_project:
            await self.pmo.swap_labels_project(pmo_id, remove=set(), add={LABEL_TRACKING})
            # projects have no issue-style comments API; recorded in the audit log
            self._audit(pmo_id, "decomposed_project", links)
        else:
            await self._feed(
                pmo_id, "issue",
                f"🧩 Decomposed into {len(drafts)} standalone issues: {links}. "
                f"This issue is canceled in their favor.")
            await self.pmo.set_status(pmo_id, "canceled")
            self._audit(pmo_id, "decomposed_canceled", links)

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
            spec_env = {
                "DEVCAKE_MISSION_ID": "",
                "DEVCAKE_MISSION_KEY": "TEAM",
                "DEVCAKE_MISSION_TYPE": "MAPPER",
                "DEVCAKE_DEV_TYPE": dev_type.name,
                "DEVCAKE_HARNESS": dev_type.harness_template,  # app-authoritative
                "DEVCAKE_SEQ": str(seq),
                "DEVCAKE_REPO_URL": self.config.repo.url,
                "DEVCAKE_DEFAULT_BRANCH": "main",
                "DEVCAKE_FORGE": self.config.repo.forge,
                "DEVCAKE_FORGE_TOKEN": self.config.repo.token,
                "DEVCAKE_EXTRA_ARGS": "",
                "DEVCAKE_MODEL": dev_type.model,
                "OTEL_EXPORTER_OTLP_ENDPOINT": f"{OO_URL}/api/{OO_ORG}/v1/traces",
                "OTEL_EXPORTER_OTLP_BASIC": _oo_basic_auth(),
            }
            env_creds, spec_files = self._credential_spec(dev_type)
            spec_env.update(env_creds)
            run = Run(
                run_id=run_id, mission_key="TEAM", mission_type="MAPPER",
                pmo_ref=self.config.pmo.id, repo_ref=self.config.repo.id,
                dev_type=dev_type.name, seq=seq,
                timeout_seconds=self.config.dev_timeout_minutes * 60,
                traceparent=traceparent, redis_password=redis_password,
                spec_env=spec_env, spec_files=spec_files,
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
        run.token_report = payload.get("token_report") or {}

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
            run.redis_password = None
            if outcome != "relations_mapped":
                exit_code = payload.get("exit_code")
                if exit_code == 12:            # DEV_AUTH breaker, same as finalize()
                    self.breakers[run.dev_type] = f"auth failure in {run.run_id}"
                run.state = "failed"
                run.error = f"mapper returned {outcome or 'failure artifact'} (exit {exit_code})"
                run.ended_at = utcnow()
                self.runs.store.save(run)
                log.warning("mapper run %s failed: %s", run.run_id, run.error)
                return
            created, rejected = await self._apply_mapper_edges(result.get("edges") or [])
            span.set_attribute("devcake.mapper.edges_created", created)
            span.set_attribute("devcake.mapper.edges_rejected", rejected)
            run.result = result
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
                f"the relation in Linear if this is wrong.")
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
        pr = await self.forge.get_pr_by_branch(f"devcake/{m.key}")
        if not pr:
            return
        state = await self.forge.pr_state(pr["number"])
        if state["merged"] or state["state"] == "closed":
            with tracer.start_as_current_span("sweep.merge") as span:
                span.set_attribute("devcake.mission.key", m.key)
                span.set_attribute("devcake.outcome",
                                   "merged" if state["merged"] else "closed")
        if state["merged"]:
            await self.pmo.swap_labels(m.pmo_id, remove={LABEL_MERGE}, add=set())
            await self.pmo.set_status(m.pmo_id, "done")
            await self._feed(
                m.pmo_id, "issue",
                f"✅ PR {state['url']} merged — mission done (merge sweep).")
            self._audit(m.pmo_id, "merge_sweep_done", state["url"])
        elif state["state"] == "closed":
            await self.pmo.swap_labels(m.pmo_id, remove={LABEL_MERGE}, add=set())
            await self.pmo.set_status(m.pmo_id, "canceled")
            await self._feed(
                m.pmo_id, "issue",
                f"🚫 PR {state['url']} was closed without merging — mission "
                f"canceled (merge sweep).")
            self._audit(m.pmo_id, "merge_sweep_canceled", state["url"])
        else:
            # advisory banner (docs/11): an open PR on DEVCAKE-MERGE awaits a
            # human — unless the deferred-retry window is actively running
            # (_deferred_merge_retry pops the entry while it drives the window)
            self.merge_handoffs[m.pmo_id] = (
                f"{m.key}: awaiting human merge — {state['url']}")
            if self.config.auto_merge:
                await self._deferred_merge_retry(m, pr, state["url"])

    async def _deferred_merge_retry(self, m: Mission, pr: dict,
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
        act = await self.pmo.get_activity(m.pmo_id)
        retry_ts = handoff_ts = None
        for e in act.entries:
            body = self._unquoted(e.body)
            if MERGE_RETRY_MARKER in body:
                retry_ts = max(retry_ts, e.ts) if retry_ts else e.ts
            if MERGE_HANDOFF_MARKER in body:
                handoff_ts = max(handoff_ts, e.ts) if handoff_ts else e.ts
        if not retry_ts or (handoff_ts and handoff_ts >= retry_ts):
            self._merge_window_closed.add(m.pmo_id)
            return  # no active retry window (auto_merge-OFF parks land here)
        window = self.config.merge_retry_window_minutes
        if (utcnow() - retry_ts).total_seconds() / 60 > window:
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
        verdict = await self.forge.mergeable(pr["number"])
        if verdict is None:
            return  # still computing / CI running — next cycle re-reads
        # a False verdict can be a non-blocking "behind" (strict up-to-date
        # rules are what make it fail) — one plain merge attempt is far
        # cheaper than an EXECUTE rework, so always try the merge first and
        # only route to rework when it actually fails on a real conflict
        try:
            await self.forge.merge(pr["number"])
        except Exception:
            if verdict is False:
                if not await self._maybe_route_conflict_to_execute(
                        m.pmo_id, m.key, pr_url, LABEL_MERGE):
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
                log.debug("deferred merge retry failed for %s", m.key,
                          exc_info=True)
            return
        await self.pmo.swap_labels(m.pmo_id, remove={LABEL_MERGE}, add=set())
        await self.pmo.set_status(m.pmo_id, "done")
        await self._feed(
            m.pmo_id, "issue",
            f"✅ Merged after deferred retry ({pr_url}). Mission done.")
        self._audit(m.pmo_id, "merge_retry_succeeded", pr_url)

    async def _tracking_sweep(self, m: Mission) -> None:
        children = await self.pmo.children_of_project(m.pmo_id)
        if children and all(c.status in ("done", "canceled") for c in children):
            with tracer.start_as_current_span("sweep.tracking") as span:
                span.set_attribute("devcake.mission.key", m.key)
            await self.pmo.set_project_status(m.pmo_id, "done")
            await self.pmo.swap_labels_project(m.pmo_id, remove={LABEL_TRACKING}, add=set())
            self._audit(m.pmo_id, "tracking_sweep_completed", f"{len(children)} children")
            log.info("project %s auto-completed (%d children done)", m.key, len(children))

    async def restore_after_failure(self, run: Run) -> None:
        """Revert the dispatch-time backlog→in_progress write after a failed attempt,
        iff the mission is still exactly as we left it (live re-read; human edits win)."""
        if run.stage_label_at_dispatch is not None or not run.mission_pmo_id:
            return  # only ONBOARD dispatches from backlog change the status
        try:
            live = await self._live(run.mission_pmo_id, run.pmo_kind)
            if live.status == "in_progress" and self._stage_of(live) is None:
                await self._set_status(run.mission_pmo_id, run.pmo_kind, "backlog")
                self._audit(run.mission_pmo_id, "set_status",
                            "backlog (restored after failed attempt)")
        except Exception:
            log.exception("status restore failed for %s", run.run_id)

    async def _post_transcript(self, run: Run, transcript: str) -> None:
        transcript = redact(transcript, [run.redis_password or ""])
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
            m = await self.pmo.get_project(pmo_id)
            md = "\n".join([
                f"# {m.key}: {m.title}",
                f"> Kind: project · Status: {m.status} · Priority: {m.priority} · URL: {m.url}",
                f"> Labels: {', '.join(sorted(m.labels)) or '(none)'}", "",
                "## Description", m.description or "(none)", "",
                "## Activity", "(projects carry no comment feed — see child issues)"])
            return {"activity_md": md, "attachments": []}
        act = await self.pmo.get_activity(pmo_id)
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
            names = dict(re.findall(r"\[([^\]]+\.\w{1,8})\]\((https://uploads\.linear\.app/\S+?)\)",
                                    e.body or ""))
            names = {v: k for k, v in names.items()}
            for url in e.attachments:
                try:
                    data = await self.pmo.download_asset(url)
                    fname = self._unique_name(
                        names.get(url) or url.rsplit("/", 1)[-1][:80] or "attachment.bin",
                        used)
                    attachments.append({"filename": fname,
                                        "content_b64": base64.b64encode(data).decode()})
                    lines.append(f"[attachment: {fname}]")
                except Exception:
                    lines.append(f"[attachment unavailable: {url}]")
            lines.append("")
        return {"activity_md": "\n".join(lines), "attachments": attachments}


# ── Relations Mapper service (docs/04 §1, ADR-0007 addendum) ─────────────────

class MapperBusy(Exception):
    """A mapper run is already active."""


class MapperUnconfigured(Exception):
    """relations_mapper.dev_type does not name an existing Dev Type."""


class MapperService:
    """Cadence + concurrency for MAPPER runs. One lock closes the manual-vs-
    interval double-dispatch window; the watermark advances only AFTER a
    successful dispatch (a transient executor error costs one poll cycle, not a
    full interval); degradation is derived from the run store — restart-safe,
    and a successful run clears it naturally."""

    def __init__(self, config: AppConfig, dev_types: dict[str, DevType],
                 mgr: MissionManager):
        self.config = config
        self.dev_types = dev_types
        self.mgr = mgr
        self._lock = asyncio.Lock()
        # first auto-run lands one interval after boot; "Run now" covers immediacy
        self._last_at = time.monotonic()

    def dev_type(self) -> DevType | None:
        rm = self.config.relations_mapper
        return self.dev_types.get(rm.dev_type) if rm.dev_type else None

    def active(self) -> bool:
        return any(r.mission_type == "MAPPER"
                   for r in self.mgr.runs.store.active())

    def degraded(self) -> str | None:
        """The 3 most recent MAPPER runs all dead ⇒ the periodic service backs
        off (docs/15). Run now stays available; a success clears the condition."""
        recent = sorted((r for r in self.mgr.runs.store.all()
                         if r.mission_type == "MAPPER"),
                        key=lambda r: r.created_at, reverse=True)[:3]
        if len(recent) == 3 and all(r.state in ("failed", "timed_out", "orphaned")
                                    for r in recent):
            return recent[0].error or "3 consecutive mapper failures"
        return None

    async def maybe_dispatch(self, missions: list[Mission]) -> None:
        """The interval path, called once per poll cycle (never while paused)."""
        rm = self.config.relations_mapper
        dt = self.dev_type()
        if not rm.enabled or dt is None:
            return
        if time.monotonic() - self._last_at < rm.interval_minutes * 60:
            return
        degraded = self.degraded()
        if degraded:
            log.warning("mapper degraded — periodic run skipped (%s)", degraded)
            return
        async with self._lock:
            if self.active():
                return
            if len(self.mgr.runs.store.active()) >= self.config.concurrency.global_max:
                return                             # counts toward the global cap
            await self.mgr.dispatch_mapper(dt, missions)
            self._last_at = time.monotonic()

    async def run_now(self) -> Run:
        """Manual trigger: works regardless of the periodic toggle and of the
        degraded state — a human pressing the button IS the reset signal."""
        dt = self.dev_type()
        if dt is None:
            raise MapperUnconfigured(
                "relations_mapper.dev_type must name an existing Dev Type — "
                "set it on the Config tab")
        async with self._lock:
            if self.active():
                raise MapperBusy("a relations-mapper run is already active")
            missions = await self.mgr.pmo.list_all(self.config.pmo.team_key)
            return await self.mgr.dispatch_mapper(dt, missions)
