"""Mission dispatch, attempt counting, credentials, activity payload (docs/04 §3.1)."""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.trace import SpanKind, Status, StatusCode

from ...harness import HARNESSES
from ...ports.forge import mission_branch
from ...telemetry import OTEL_COLLECTOR_URL
from ...config import DevType
from ..model import (Activity, LABEL_FAILED, Mission, MissionRef, MissionType,
                     STAGE_LABELS, derive)
from ..run import Run, utcnow
from . import markers
from .markers import FEED_INLINE_MAX, STEP_MARKER

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


def _resolve_repo(self, mission: Mission, all_runs: list | None = None):
    """(repo_name | None, gate_reason | None) — marker > instance default >
    zero-repo gate, STICKY once a run exists (domain/repo_routing.py).
    `all_runs`: pre-fetched store snapshot — the poll loop stamps every
    mission every cycle, so it reads the store ONCE per segment instead of
    once per mission; dispatch's live re-resolve reads fresh."""
    from ..repo_routing import resolve_repo
    if all_runs is None:
        all_runs = self.runs.store.all()
    history = sorted(
        (r for r in all_runs
         if r.mission_pmo_id == mission.pmo_id and self._run_is_ours(r)
         and r.mission_type != "MAPPER"),
        key=lambda r: r.created_at, reverse=True)
    return resolve_repo(mission, self.instance,
                        set(self.forges.instances), history)


def _mapper_repo(self) -> str | None:
    """The repo a MAPPER run clones (the entrypoint always clones): the
    instance's default repo when configured, else any configured repo."""
    if self.instance.default_repo in self.forges.instances:
        return self.instance.default_repo
    return next(iter(self.forges.instances), None)


async def dispatch(self, mission: Mission, mtype: MissionType,
                   dev_type: DevType) -> Run | None:
    live = await self.pmo.get(mission.ref)                     # live re-read
    d = derive(live, self.config.adoption_mode)
    if d.mission_type != mtype:
        return None                                            # world moved on
    # per-mission repo resolution, re-checked LIVE at dispatch (M10; sticky —
    # a mid-mission routing change gates instead of re-routing, plan H3)
    repo_name, gate_reason = self._resolve_repo(live)
    if repo_name is None:
        self.blocked_reasons[live.pmo_id] = gate_reason
        log.info("dispatch of %s refused — %s", live.key, gate_reason)
        return None
    repo = self.forges.instance(repo_name)
    forge = self.forges.get(repo_name)
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
    from ..ids import make_run_id
    run_id = make_run_id(self.instance_name, mission.key, seq, mtype.value)

    with tracer.start_as_current_span("mission.dispatch", kind=SpanKind.PRODUCER) as span:
        span.set_attribute("devcake.run.id", run_id)
        span.set_attribute("devcake.mission.key", mission.key)
        span.set_attribute("devcake.mission.type", mtype.value)
        span.set_attribute("devcake.dev_type", dev_type.name)
        span.set_attribute("devcake.run.attempt", attempt)
        carrier: dict[str, str] = {}
        inject(carrier)
        traceparent = carrier.get("traceparent", "")

        from ...prompts import (execute_prompt, onboard_prompt, plan_prompt,
                              review_prompt)
        repo_slug = repo.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        prompt = {
            MissionType.ONBOARD: lambda: onboard_prompt(dev_type.identifying_prompt, live),
            MissionType.PLAN: lambda: plan_prompt(dev_type.identifying_prompt, live),
            MissionType.EXECUTE: lambda: execute_prompt(
                dev_type.identifying_prompt, live, repo_slug,
                pr_instructions=forge.descriptor.pr_instructions,
                default_branch=repo.default_branch),
            MissionType.REVIEW: lambda: review_prompt(dev_type.identifying_prompt, live),
        }[mtype]()

        spec_env = self._protocol_spec_env(
            mission_id=mission.pmo_id, mission_key=mission.key,
            mission_type=mtype.value, dev_type=dev_type, seq=seq,
            extra_args=assignment.extra_cli_args, repo=repo, forge=forge)
        run = Run(
            run_id=run_id, mission_key=mission.key, mission_type=mtype.value,
            pmo_kind=mission.pmo_kind,
            pmo_ref=self.instance_name, repo_ref=repo_name,
            dev_type=dev_type.name, seq=seq, attempt_of_step=attempt,
            timeout_seconds=self.config.dev_timeout_minutes * 60,
            traceparent=traceparent,
            spec_env=spec_env,
        )
        run.spec_prompt = prompt
        run.branch = mission_branch(self.instance_name, mission.key)
        run.stage_label_at_dispatch = self._stage_of(live)
        run.mission_pmo_id = mission.pmo_id
        await self.runs.bootstrap.launch(
            run, image=HARNESSES[dev_type.harness_template].image)

        if live.status == "backlog":
            await self.pmo.set_status(mission.ref, "in_progress")
            self._audit(mission.pmo_id, "set_status", "in_progress")
        log.info("dispatched %s (attempt %d, dev=%s)", run_id, attempt, dev_type.name)
        return run


def _protocol_spec_env(self, *, mission_id: str, mission_key: str,
                       mission_type: str, dev_type: DevType, seq: int,
                       extra_args: str, repo, forge) -> dict[str, str]:
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
        "DEVCAKE_REPO_URL": repo.url,
        "DEVCAKE_DEFAULT_BRANCH": repo.default_branch,
        "DEVCAKE_CLONE_USER": forge.descriptor.clone_user,
        "DEVCAKE_GIT_NAME": forge.descriptor.git_user_name,
        "DEVCAKE_GIT_EMAIL": forge.descriptor.git_email,
        "DEVCAKE_FORGE_CLI_ENVS": ",".join(forge.descriptor.cli_token_envs),
        "DEVCAKE_EXTRA_ARGS": extra_args,
        "DEVCAKE_MODEL": dev_type.model,
        # Devs export through the collector, credential-free (ISSUES #13)
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"{OTEL_COLLECTOR_URL}/v1/traces",
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
    repo = self.forges.instance(run.repo_ref)
    if repo is None:
        # the run's repo vanished from config mid-flight → runspec.error
        # (the resolution-failure contract, domain/forge_runtime.py)
        log.error("runspec for %s refused: repo %r is no longer configured",
                  run.run_id, run.repo_ref)
        return None
    env: dict[str, str] = {**env_creds}
    write = repo.token
    ro = repo.token_ro
    if run.mission_type == "EXECUTE":
        env["DEVCAKE_FORGE_TOKEN"] = write
    else:
        env["DEVCAKE_FORGE_TOKEN"] = ro or write
    return {"env": env, "credential_files": spec_files}


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


def _derive_seq(activity) -> int:
    """docs/02 §8 — count prior step artifacts in the feed + 1."""
    steps = [int(m.group(1)) for e in activity.entries
             for m in STEP_MARKER.finditer(e.body or "")]
    return (max(steps) + 1) if steps else 1


def _unique_name(name: str, used: set[str]) -> str:
    """docs/07 §2 collision rule: later duplicates get -2, -3, … suffixes."""
    stem, dot, ext = name.rpartition(".")
    cand, i = name, 1
    while cand in used:
        i += 1
        cand = f"{stem}-{i}.{ext}" if dot else f"{name}-{i}"
    used.add(cand)
    return cand


def _aware(ts: datetime) -> datetime:
    """Anchor timestamps come from three sources (audit log, run records,
    PMO comments); a stray naive one must not crash the scheduler."""
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _last_giveup_at(cls, pmo_id: str) -> datetime | None:
    try:
        ts = None
        with open(markers.AUDIT_PATH) as f:
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
    all_runs = [r for r in self.runs.store.all()
                if r.mission_pmo_id == pmo_id and self._run_is_ours(r)]
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

