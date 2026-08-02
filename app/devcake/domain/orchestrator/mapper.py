"""MAPPER mission dispatch/finalize and edge application (ADR-0007)."""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.trace import SpanKind

from ...harness import HARNESSES
from ...security import redact_value
from ...config import DevType
from .. import costing
from ..model import LABEL_OPTIN, Mission
from . import dispatch
from ..run import Run, utcnow

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


async def dispatch_mapper(mgr, dev_type: DevType, missions: list[Mission]) -> Run:
    """Dispatch a MAPPER run: a Dev whose only job is proposing missing
    blocked-by edges. No PMO writes at dispatch (no status, no labels) —
    finalize_mapper validates and applies whatever it proposes."""
    from ..ids import make_run_id
    from ...prompts import MAPPER_MISSION_CAP, mapper_prompt
    repo_name = dispatch.mapper_repo(mgr)
    if repo_name is None:
        # spec env carries the forge dialect — no repo, no mapper runs either
        raise RuntimeError("no repository configured — mapper runs need the "
                           "forge dialect in their run spec")
    repo, forge = mgr.forges.instance(repo_name), mgr.forges.get(repo_name)
    eligible = [m for m in missions
                if m.pmo_kind == "issue" and m.status not in ("done", "canceled")
                and (mgr.config.adoption_mode != "opt_in"
                     or LABEL_OPTIN in m.labels)]
    if len(eligible) > MAPPER_MISSION_CAP:
        log.warning("mapper prompt truncated to %d of %d missions",
                    MAPPER_MISSION_CAP, len(eligible))
    # own-instance MAPPER runs only (audit A29, cosmetic): run ids carry the
    # instance prefix + random suffix, so no collision — but a cross-instance
    # count made the human-visible seq misleading
    seq = 1 + sum(1 for r in mgr.runs.store.all()
                  if r.mission_type == "MAPPER" and mgr._run_is_ours(r))
    run_id = make_run_id(mgr.instance_name, "TEAM", seq, "MAPPER")

    with tracer.start_as_current_span("mission.dispatch", kind=SpanKind.PRODUCER) as span:
        span.set_attribute("devcake.run.id", run_id)
        span.set_attribute("devcake.mission.key", "TEAM")
        span.set_attribute("devcake.mission.type", "MAPPER")
        span.set_attribute("devcake.dev_type", dev_type.name)
        carrier: dict[str, str] = {}
        inject(carrier)
        traceparent = carrier.get("traceparent", "")

        spec_env = dispatch._protocol_spec_env(
            mgr,
            recover_misplaced_result=mgr.config.recover_misplaced_result,
            continuation_policy=mgr.config.continuation_policy,
            max_continuations=mgr.config.max_continuations,
            mission_id="", mission_key="TEAM", mission_type="MAPPER",
            dev_type=dev_type, seq=seq, extra_args="",
            repo=repo, forge=forge)
        run = Run(
            run_id=run_id, mission_key="TEAM", mission_type="MAPPER",
            pmo_ref=mgr.instance_name, repo_ref=repo_name,
            dev_type=dev_type.name, seq=seq,
            timeout_seconds=mgr.config.dev_timeout_minutes * 60,
            traceparent=traceparent,
            spec_env=spec_env,
        )
        run.spec_skills = await dispatch._skill_payload(mgr, dev_type)
        run.spec_skills_dir = HARNESSES[dev_type.harness_template].skills_dir or ""
        run.spec_prompt = dispatch.append_required_skills(
            mapper_prompt(dispatch._identifying_prompt(mgr, dev_type), eligible),
            dev_type.skills_required, run.spec_skills)
        await mgr.runs.bootstrap.launch(
            run, image=HARNESSES[dev_type.harness_template].image)
        log.info("dispatched mapper %s (dev=%s, %d missions in prompt)",
                 run_id, dev_type.name, len(eligible))
        return run


async def finalize_mapper(mgr, run: Run, payload: dict) -> None:
    """MAPPER runs have no host mission: no transcript/token-report comments —
    the output lands as relations + a notification comment on each blocked
    mission. Failures are logged only; the next interval simply retries."""
    result = payload.get("result") or {}
    outcome = result.get("outcome", "")
    # same ADR-0021 stamp as mission finalize — mapper spend is fleet spend
    run.token_report = redact_value(costing.stamp_estimate(
        payload.get("token_report") or {}, mgr.config.cost_inputs))
    try:                                               # ADR-0022, as finalize()
        run.continuations_used = int(payload.get("continuations_used") or 0)
    except (TypeError, ValueError):
        run.continuations_used = 0

    ctx = None
    if run.traceparent:
        from opentelemetry.propagate import extract
        ctx = extract({"traceparent": run.traceparent})
    with tracer.start_as_current_span("run.finalize", context=ctx,
                                      kind=SpanKind.CONSUMER) as span:
        span.set_attribute("devcake.run.id", run.run_id)
        span.set_attribute("devcake.outcome", outcome or "(failure artifact)")
        await mgr.messaging.delete_run_user(run.run_id)
        await mgr.messaging.delete_reply_stream(run.run_id)
        if outcome != "relations_mapped":
            run.state = "failed"
            run.error = mgr.dev_failure_error(run, payload)
            run.ended_at = utcnow()
            mgr.runs.store.save(run)
            log.warning("mapper run %s failed: %s", run.run_id, run.error)
            return
        created, rejected = await apply_mapper_edges(mgr, result.get("edges") or [])
        span.set_attribute("devcake.mapper.edges_created", created)
        span.set_attribute("devcake.mapper.edges_rejected", rejected)
        run.result = redact_value(result)
        run.state, run.ended_at = "finished", utcnow()
        mgr.runs.store.save(run)
        log.info("mapper %s finished: %d edges created, %d rejected",
                 run.run_id, created, rejected)


async def apply_mapper_edges(mgr, edges: list) -> tuple[int, int]:
    """The Dev is advisory; the app is the gatekeeper — drop edges that are
    unknown, self-referential, terminal, duplicate, or cycle-forming (ADR-0007)."""
    missions = await mgr.pmo.list_all(mgr.instance.team_key)
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
        elif _creates_cycle(graph, blocker.pmo_id, blocked.pmo_id):
            reason = "would create a cycle"
        if reason:
            rejected += 1
            mgr._audit(blocked.pmo_id if blocked else "", "mapper_edge_rejected",
                        f"{blocker_key}→{blocked_key}: {reason}")
            log.info("mapper edge %s blocks %s rejected: %s",
                     blocker_key, blocked_key, reason)
            continue
        await mgr.pmo.create_relation(blocker.pmo_id, blocked.pmo_id)
        graph.setdefault(blocked.pmo_id, set()).add(blocker.pmo_id)
        mgr._audit(blocked.pmo_id, "relation_created",
                    f"mapper: blocked by {blocker.key}")
        await mgr._feed(
            blocked.pmo_id, "issue",
            f"🔗 DevCake mapped a blocking relation: this mission is blocked by "
            f"**{blocker.key}** and will not start before it finishes. Remove "
            f"the relation in the PMO if this is wrong.")
        created += 1
    return created, rejected


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

