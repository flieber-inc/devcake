"""Authoritative ``devcake.*`` OTel attribute vocabulary (docs/12 §3, CAKE-86).

Data-only chokepoint: every ``span.set_attribute("devcake.…")`` name that is
queryable or cross-span (alerts, dashboards, SPA, aggregation docs) must appear
in ``ATTRIBUTES``. OpenObserve flattens dots to underscores — use ``oo_field``.

Span home constants pin the documented carriers for attrs that dashboards and
alerts filter on; they are not an SDK import surface.
"""

from __future__ import annotations

# Normative names exactly as set_attribute keys (dots, not OO underscores).
ATTRIBUTES: frozenset[str] = frozenset({
    # Identity / correlation
    "devcake.mission.key",
    "devcake.mission.type",
    "devcake.dev_type",
    "devcake.harness",
    "devcake.cli_version",
    "devcake.run.id",
    "devcake.run.attempt",
    "devcake.instance",
    "devcake.pmo.id",
    "devcake.repo",
    "devcake.kind",
    # Tokens / cost (home: run.finalize)
    "devcake.tokens.input",
    "devcake.tokens.output",
    "devcake.tokens.total",
    "devcake.tokens.cache_read",
    "devcake.tokens.cache_write",
    "devcake.tokens.reasoning",
    "devcake.cost.usd",
    "devcake.cost.usd_estimated",
    "devcake.cost.rate_card",
    # Outcomes / verdicts
    "devcake.outcome",
    "devcake.verdict",
    "devcake.cause",
    "devcake.reason",
    "devcake.error.class",  # mission.give_up last counted failure (CAKE-75)
    "devcake.kill.reason",
    "devcake.merge.verdict",
    # Poll cycle counts
    "devcake.poll.cycle",
    "devcake.missions.seen",
    "devcake.missions.candidates",
    "devcake.missions.dispatched",
    # Continuations (ADR-0022)
    "devcake.continuation",
    "devcake.continuations",
    # Audit / breaker / poison
    "devcake.audit.action",
    "devcake.audit.detail",
    "devcake.breaker",
    "devcake.poison.entries",
    "devcake.poison.reason",
    # PMO request budget (ADR-0040)
    "devcake.pmo.call_class",
    "devcake.pmo.wait_s",
    "devcake.pmo.remaining",
    "devcake.pmo.reason",
    "devcake.pmo.budget_refused",
    # Steward / discoveries
    "devcake.steward.duty",
    "devcake.steward.edges_created",
    "devcake.steward.edges_rejected",
    "devcake.steward.routes_delivered",
    "devcake.steward.routes_rejected",
    "devcake.discoveries.harvested",
    # Clear-runs / baker / sweep
    "devcake.clear.runs_deleted",
    "devcake.clear.dagu_deleted",
    "devcake.clear.ok",
    "devcake.baker.cause",
    "devcake.baker.detail",
    "devcake.baker.state",
    "devcake.children",
})

TOKEN_ATTRS: frozenset[str] = frozenset(
    name for name in ATTRIBUTES if name.startswith("devcake.tokens.")
)

# Dashboard / alert home spans (pinned; emitters already match).
COST_HOME_SPAN = "run.finalize"
PMO_TRANSIENT_SPAN = "poll.instance"


def oo_field(name: str) -> str:
    """OpenObserve SQL column: dots → underscores (devcake.run.id → devcake_run_id)."""
    return name.replace(".", "_")
