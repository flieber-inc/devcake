"""Mission scheduling and finalization (docs/04) — M3 scope: ONBOARD on Issues.

PLAN/EXECUTE/REVIEW dispatch and the trivial/decompose finalizations land at
M4/M5; the mechanics here (ordering, caps, grace cycle, compare-and-transition,
attempt counting) are the permanent ones.
"""

import base64
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.trace import SpanKind

from .config import AppConfig, DevType
from .linear import LinearAdapter
from .messaging import Messaging
from .forge import GitHubForge
from .forge_gitlab import GitLabForge
from .pmo import (LABEL_CREATED, LABEL_EXECUTE, LABEL_FAILED, LABEL_MERGE,
                  LABEL_OPTIN, LABEL_PLAN, LABEL_REVIEW, LABEL_SKIP, LABEL_TRACKING,
                  Mission, MissionType, PRIORITY_RANK, STAGE_LABELS, derive)
from .runs import RunManager
from .state import Run, utcnow
from .telemetry import OO_ORG, OO_URL

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")

# M5: the full state machine is dispatchable, projects included (ADR-0006).
DISPATCHABLE_TYPES = {MissionType.ONBOARD, MissionType.PLAN,
                      MissionType.EXECUTE, MissionType.REVIEW}

STEP_MARKER = re.compile(r"`(\d+)_(ONBOARD|PLAN|EXECUTE|REVIEW)\.md`")

AUDIT_PATH = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "state" / "events.jsonl"


def _oo_basic_auth() -> str:
    email = os.environ.get("OO_ROOT_EMAIL", "")
    password = os.environ.get("OO_ROOT_PASSWORD", "")
    return base64.b64encode(f"{email}:{password}".encode()).decode()


class MissionManager:
    def __init__(self, config: AppConfig, dev_types: dict[str, DevType],
                 pmo: LinearAdapter, runs: RunManager, messaging: Messaging):
        self.config = config
        self.dev_types = dev_types
        self.pmo = pmo
        self.runs = runs
        self.messaging = messaging
        self._grace: set[str] = set()       # pmo_ids we transitioned last cycle
        self._grace_next: set[str] = set()
        self.breakers: dict[str, str] = {}  # dev_type → reason (DEV_AUTH circuit breaker)
        self.forge = self._make_forge()

    def _make_forge(self):
        reviewer = os.environ.get(self.config.repo.reviewer_token_env or "") or None
        cls = GitLabForge if self.config.repo.forge == "gitlab" else GitHubForge
        return cls(self.config.repo.url, self.config.repo.token, reviewer)

    def reload_forge(self) -> None:
        """Called after a config write changes repo settings (hot reload)."""
        self.forge = self._make_forge()

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

    async def schedule(self, missions: list[Mission]) -> int:
        candidates = []
        for m in missions:
            d = derive(m, self.config.adoption_mode)
            if not d.schedulable or d.mission_type not in DISPATCHABLE_TYPES:
                continue
            if m.pmo_id in self._grace:
                continue  # grace cycle after our own writes (docs/04 §2)
            if any(r.mission_pmo_id == m.pmo_id for r in self.runs.store.active()):
                continue  # in-flight guard
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

    # ── dispatch (docs/04 §3.1) ──────────────────────────────────────────────

    async def dispatch(self, mission: Mission, mtype: MissionType,
                       dev_type: DevType) -> Run | None:
        live = await self._live(mission.pmo_id, mission.pmo_kind)  # live re-read
        d = derive(live, self.config.adoption_mode)
        if d.mission_type != mtype:
            return None                                            # world moved on

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
            from .prompts import (execute_prompt, onboard_prompt, plan_prompt,
                                  review_prompt)
            repo_name = self.config.repo.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
            prompt = {
                MissionType.ONBOARD: lambda: onboard_prompt(dev_type.identifying_prompt, live),
                MissionType.PLAN: lambda: plan_prompt(dev_type.identifying_prompt, live),
                MissionType.EXECUTE: lambda: execute_prompt(dev_type.identifying_prompt,
                                                            live, repo_name),
                MissionType.REVIEW: lambda: review_prompt(dev_type.identifying_prompt, live),
            }[mtype]()

            spec_env = {
                "DEVCAKE_MISSION_ID": mission.pmo_id,
                "DEVCAKE_MISSION_KEY": mission.key,
                "DEVCAKE_MISSION_TYPE": mtype.value,
                "DEVCAKE_DEV_TYPE": dev_type.name,
                "DEVCAKE_SEQ": str(seq),
                "DEVCAKE_REPO_URL": self.config.repo.url,
                "DEVCAKE_DEFAULT_BRANCH": "main",
                "DEVCAKE_FORGE": self.config.repo.forge,
                "DEVCAKE_FORGE_TOKEN": self.config.repo.token,
                "DEVCAKE_EXTRA_ARGS": assignment.extra_cli_args,
                "OTEL_EXPORTER_OTLP_ENDPOINT": f"{OO_URL}/api/{OO_ORG}/v1/traces",
                "OTEL_EXPORTER_OTLP_BASIC": _oo_basic_auth(),
            }
            for var in dev_type.credential_env:                    # harness credentials
                if os.environ.get(var):
                    spec_env[var] = os.environ[var]

            spec_files = []
            secrets_dir = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "secrets" / dev_type.name
            for cf in dev_type.credential_files:
                p = secrets_dir / cf.secret_file
                if p.exists():
                    spec_files.append({"path_hint": cf.path_hint,
                                       "content": p.read_text(), "mode": "600"})
                else:
                    log.warning("credential file %s missing for %s — run scripts/%s login",
                                p, dev_type.name, dev_type.harness_template.split("-")[0])
            run = Run(
                run_id=run_id, mission_key=mission.key, mission_type=mtype.value,
                pmo_kind=mission.pmo_kind,
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
                params={"RUN_ID": run_id, "IMAGE": dev_type.docker_image,
                        "TRACEPARENT": traceparent,
                        "REDIS_USER": f"dev-{run_id}", "REDIS_PASSWORD": redis_password},
                dag_run_id=run_id)

            if live.status == "backlog":
                await self._set_status(mission.pmo_id, mission.pmo_kind, "in_progress")
                self._audit(mission.pmo_id, "set_status", "in_progress")
            log.info("dispatched %s (attempt %d, dev=%s)", run_id, attempt, dev_type.name)
            return run

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
        """Projects have no issue-style comments API (verified at M2/M5): their
        run artifacts live in the audit log + OpenObserve; the substance lands on
        the child issues anyway (ADR-0006)."""
        if kind == "project":
            self._audit(pmo_id, "project_feed_suppressed", markdown[:120])
        else:
            await self.pmo.post_comment(pmo_id, markdown)

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
                await self.restore_after_failure(run)
                log.warning("run %s failed (exit %s, attempt %d)",
                            run.run_id, exit_code, run.attempt_of_step)
                return

            # 3 — compare-and-transition
            if "transition" not in run.finalized_steps:
                await self._transition(run, result, plan_md)
                run.finalized_steps.append("transition")
                self.runs.store.save(run)

            await self.messaging.delete_run_user(run.run_id)
            await self.messaging.delete_reply_stream(run.run_id)
            run.result = result
            run.state, run.ended_at = "finished", utcnow()
            run.redis_password = None
            self.runs.store.save(run)
            log.info("finalized %s (%s)", run.run_id, outcome)

    async def _transition(self, run: Run, result: dict, plan_md: str | None) -> None:
        outcome = result.get("outcome", "")
        pmo_id = run.mission_pmo_id
        live = await self._live(pmo_id, run.pmo_kind)               # live re-read
        if run.pmo_kind == "project" and outcome != "decomposed":
            await self._swap(pmo_id, "project", remove=set(), add={LABEL_SKIP})
            self._audit(pmo_id, "project_bad_outcome_parked", outcome)
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
            log.warning("EXTERNAL_TRANSITION on %s — no labels applied", run.run_id)
            return

        if outcome == "planned":
            url = await self.pmo.upload_attachment(pmo_id, f"PLAN_{run.seq}.md",
                                                   (plan_md or "").encode())
            await self.pmo.post_comment(
                pmo_id, f"📋 DevCake plan for this mission: [PLAN_{run.seq}.md]({url})")
            await self.pmo.swap_labels(pmo_id, remove={LABEL_PLAN}, add={LABEL_EXECUTE})
            self._audit(pmo_id, "label_swap", f"{LABEL_PLAN}→{LABEL_EXECUTE}")
        elif outcome == "executed":
            from .pmo import LABEL_REVIEW
            await self.pmo.swap_labels(pmo_id, remove={LABEL_EXECUTE}, add={LABEL_REVIEW})
            await self.pmo.post_comment(
                pmo_id, f"🔀 DevCake opened/updated the pull request: "
                        f"{result.get('pr_url', '(no url reported)')} — awaiting REVIEW.")
            self._audit(pmo_id, "label_swap", f"{LABEL_EXECUTE}→{LABEL_REVIEW}")
        elif outcome == "executed_trivially":
            from .pmo import LABEL_REVIEW
            await self.pmo.swap_labels(pmo_id, remove=set(), add={LABEL_REVIEW})
            await self.pmo.post_comment(
                pmo_id, f"🔀 Trivial path: PR opened ({result.get('pr_url', '?')}) — "
                        f"the trivial path never skips REVIEW (docs/03 §1.1).")
            self._audit(pmo_id, "label_add", LABEL_REVIEW)
        elif outcome == "reviewed":
            await self._finalize_review(run, result)
        elif outcome == "decomposed":
            await self._finalize_decomposition(run, result)
        elif outcome == "plan_needed" and plan_md:
            url = await self.pmo.upload_attachment(pmo_id, f"PLAN_{run.seq}.md",
                                                   plan_md.encode())
            await self.pmo.post_comment(
                pmo_id, f"📋 DevCake attached an opportunistic plan from triage: "
                        f"[PLAN_{run.seq}.md]({url}) — skipping the PLAN step.")
            await self.pmo.swap_labels(pmo_id, remove=set(), add={LABEL_EXECUTE})
            self._audit(pmo_id, "label_add", LABEL_EXECUTE)
        elif outcome == "plan_needed":
            await self.pmo.swap_labels(pmo_id, remove=set(), add={LABEL_PLAN})
            self._audit(pmo_id, "label_add", LABEL_PLAN)
        else:
            await self.pmo.swap_labels(pmo_id, remove=set(), add={LABEL_SKIP})
            await self.pmo.post_comment(
                pmo_id, f"ℹ️ DevCake received unknown outcome `{outcome}` — parked with "
                        f"`DEVCAKE-SKIP` for a human to inspect.")
            self._audit(pmo_id, "label_add", f"{LABEL_SKIP} (unknown outcome {outcome})")

    # ── REVIEW finalization (docs/03 §4, merge-before-Done) ──────────────────

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
                    await self.pmo.post_comment(
                        pmo_id, f"✅ REVIEW approved; PR merged ({pr_url}). Mission done.")
                    self._audit(pmo_id, "review_approve_merged", pr_url)
                except Exception as e:
                    await self.pmo.swap_labels(pmo_id, remove={LABEL_REVIEW},
                                               add={LABEL_MERGE})
                    await self.pmo.post_comment(
                        pmo_id, f"⚠️ REVIEW approved but auto-merge failed ({e}); "
                                f"awaiting human merge of {pr_url} (`DEVCAKE-MERGE`).")
                    self._audit(pmo_id, "review_approve_merge_failed", str(e)[:120])
            else:
                await self.pmo.swap_labels(pmo_id, remove={LABEL_REVIEW},
                                           add={LABEL_MERGE})
                await self.pmo.post_comment(
                    pmo_id, f"✅ REVIEW approved "
                            f"({'formal approval filed' if formal else 'APPROVED-BY-DEVCAKE marker'}). "
                            f"Awaiting human merge of {pr_url} — the merge sweep completes "
                            f"this mission once it merges." + footer)
                self._audit(pmo_id, "review_approve_awaiting_merge", pr_url)
        else:  # reject
            rejections = 1 + sum(
                1 for r in self.runs.store.all()
                if r.mission_pmo_id == pmo_id and r.mission_type == "REVIEW"
                and r.state == "finished" and (r.result or {}).get("verdict") == "reject")
            await self.pmo.post_comment(
                pmo_id, f"🔁 REVIEW rejected (round {rejections}) — back to EXECUTE.\n\n"
                        + report)
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
                await self.pmo.post_comment(pmo_id, warn)
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
        is_project = live.pmo_kind == "project"
        existing = set()
        if is_project:
            existing = {m.title for m in await self.pmo.children_of_project(pmo_id)}
        labels = {LABEL_CREATED}
        if self.config.adoption_mode == "opt_in":
            labels.add(LABEL_OPTIN)
        created = []
        for i, d in enumerate(drafts, start=1):
            title = d.get("title", f"part {i}")
            if title in existing:
                continue                                          # idempotent top-up
            footer = f"\n\n---\n_Created by DevCake from {live.key} — part {i}/{len(drafts)}_"
            key = await self.pmo.create_mission(
                self.config.pmo.team_key, title,
                (d.get("description") or "") + footer,
                d.get("priority") or "medium", labels,
                project_id=pmo_id if is_project else None)
            created.append(key)
        links = ", ".join(created) or "(all already existed)"
        if is_project:
            await self.pmo.swap_labels_project(pmo_id, remove=set(), add={LABEL_TRACKING})
            # projects have no issue-style comments API; recorded in the audit log
            self._audit(pmo_id, "decomposed_project", links)
        else:
            await self.pmo.post_comment(
                pmo_id, f"🧩 Decomposed into {len(drafts)} standalone issues: {links}. "
                        f"This issue is canceled in their favor.")
            await self.pmo.set_status(pmo_id, "canceled")
            self._audit(pmo_id, "decomposed_canceled", links)

    # ── poll-cycle sweeps (docs/04 §1) ───────────────────────────────────────

    async def sweeps(self, missions: list[Mission]) -> None:
        for m in missions:
            try:
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
        if state["merged"]:
            await self.pmo.swap_labels(m.pmo_id, remove={LABEL_MERGE}, add=set())
            await self.pmo.set_status(m.pmo_id, "done")
            await self.pmo.post_comment(
                m.pmo_id, f"✅ PR {state['url']} merged — mission done (merge sweep).")
            self._audit(m.pmo_id, "merge_sweep_done", state["url"])
        elif state["state"] == "closed":
            await self.pmo.swap_labels(m.pmo_id, remove={LABEL_MERGE}, add=set())
            await self.pmo.set_status(m.pmo_id, "canceled")
            await self.pmo.post_comment(
                m.pmo_id, f"🚫 PR {state['url']} was closed without merging — mission "
                          f"canceled (merge sweep).")
            self._audit(m.pmo_id, "merge_sweep_canceled", state["url"])

    async def _tracking_sweep(self, m: Mission) -> None:
        children = await self.pmo.children_of_project(m.pmo_id)
        if children and all(c.status in ("done", "canceled") for c in children):
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
        name = f"{run.seq}_{run.mission_type}.md"
        body = f"🧾 DevCake transcript `{name}` (run `{run.run_id}`)\n\n---\n\n{transcript}"
        if run.pmo_kind == "project":
            await self._feed(run.mission_pmo_id, "project", body)
            return
        if len(body.encode()) > 50 * 1024:                          # docs/05 §4 threshold
            url = await self.pmo.upload_attachment(run.mission_pmo_id, name,
                                                   transcript.encode())
            body = (f"🧾 DevCake transcript `{name}` (run `{run.run_id}`) — "
                    f"too large for a comment, attached: [{name}]({url})")
        await self.pmo.post_comment(run.mission_pmo_id, body)
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
        ]
        attachments = []
        for e in act.entries:
            body = e.body or ""
            if len(body) > 2048:                                    # externalize long bodies
                fname = f"entry-{e.ts:%Y%m%dT%H%M%S}.md"
                attachments.append({"filename": fname,
                                    "content_b64": base64.b64encode(body.encode()).decode()})
                body = body[:300].replace("\n", " ") + f"… — see: {fname}"
            lines.append(f"### {e.ts:%Y-%m-%d %H:%M} — {e.author} ({e.kind})")
            lines.append(body)
            names = dict(re.findall(r"\[([^\]]+\.\w{1,8})\]\((https://uploads\.linear\.app/\S+?)\)",
                                    e.body or ""))
            names = {v: k for k, v in names.items()}
            for url in e.attachments:
                try:
                    data = await self.pmo.download_asset(url)
                    fname = names.get(url) or url.rsplit("/", 1)[-1][:80] or "attachment.bin"
                    attachments.append({"filename": fname,
                                        "content_b64": base64.b64encode(data).decode()})
                    lines.append(f"[attachment: {fname}]")
                except Exception:
                    lines.append(f"[attachment unavailable: {url}]")
            lines.append("")
        return {"activity_md": "\n".join(lines), "attachments": attachments}
