"""MAPPER mission dispatch/finalize and edge application (ADR-0007)."""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.trace import SpanKind

from ...harness import HARNESSES
from ...security import redact_value
from ...config import DevType
from ..model import LABEL_OPTIN, Mission
from ..run import Run, utcnow

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


async def dispatch_mapper(self, dev_type: DevType, missions: list[Mission]) -> Run:
    """Dispatch a MAPPER run: a Dev whose only job is proposing missing
    blocked-by edges. No PMO writes at dispatch (no status, no labels) —
    finalize_mapper validates and applies whatever it proposes."""
    from ..ids import make_run_id
    from ...prompts import MAPPER_MISSION_CAP, mapper_prompt
    eligible = [m for m in missions
                if m.pmo_kind == "issue" and m.status not in ("done", "canceled")
                and (self.config.adoption_mode != "opt_in"
                     or LABEL_OPTIN in m.labels)]
    if len(eligible) > MAPPER_MISSION_CAP:
        log.warning("mapper prompt truncated to %d of %d missions",
                    MAPPER_MISSION_CAP, len(eligible))
    seq = 1 + sum(1 for r in self.runs.store.all() if r.mission_type == "MAPPER")
    run_id = make_run_id(self.instance_name, "TEAM", seq, "MAPPER")

    with tracer.start_as_current_span("mission.dispatch", kind=SpanKind.PRODUCER) as span:
        span.set_attribute("devcake.run.id", run_id)
        span.set_attribute("devcake.mission.key", "TEAM")
        span.set_attribute("devcake.mission.type", "MAPPER")
        span.set_attribute("devcake.dev_type", dev_type.name)
        carrier: dict[str, str] = {}
        inject(carrier)
        traceparent = carrier.get("traceparent", "")

        spec_env = self._protocol_spec_env(
            mission_id="", mission_key="TEAM", mission_type="MAPPER",
            dev_type=dev_type, seq=seq, extra_args="")
        run = Run(
            run_id=run_id, mission_key="TEAM", mission_type="MAPPER",
            pmo_ref=self.config.pmos[0].name, repo_ref=self.config.repos[0].name,
            dev_type=dev_type.name, seq=seq,
            timeout_seconds=self.config.dev_timeout_minutes * 60,
            traceparent=traceparent,
            spec_env=spec_env,
        )
        run.spec_prompt = mapper_prompt(dev_type.identifying_prompt, eligible)
        await self.runs.bootstrap.launch(
            run, image=HARNESSES[dev_type.harness_template].image)
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
            run.error = self.dev_failure_error(run, payload)
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
    missions = await self.pmo.list_all(self.instance.team_key)
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

