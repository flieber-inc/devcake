"""Runs list/detail responses (ADR-0021 part 3) — application-service module
per ADR-0015 Decision 3: the main.py routes are thin forwards here.

Row token/cost fields are an explicit SCALAR allowlist extracted from the
persisted token_report — the dict itself (with `notes`), prompts, results,
and credential material are never serialized (docs/11 §1). Estimates are
recomputed at read time from the CURRENT config.cost_inputs so a Cost
Inputs edit changes the Runs tab on its next poll; the finalize-time stamp
(with its own rate_card_id vintage) remains the historical record in the
feed and OTel (adr/0021 §4). One store.all() scan serves rows, totals, and
the pmo_refs filter options.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException

from ..config import CostInputs
from ..domain import costing
from ..domain.run import Run

_TOKEN_SUMS = ("input_tokens", "output_tokens", "cache_read_tokens",
               "cache_write_tokens", "total_tokens")

_LIST_FIELDS = {"run_id", "mission_key", "mission_type", "dev_type", "seq",
                "state", "created_at", "started_at", "ended_at", "error",
                "error_class", "attempt_counted", "verdict"}

_DETAIL_FIELDS = _LIST_FIELDS | {
    "schema_version", "mission_pmo_id", "pmo_kind", "pmo_ref", "repo_ref",
    "attempt_of_step", "stage_label_at_dispatch", "last_heartbeat",
    "timeout_seconds", "finalized_steps", "artifact_bytes"}


def _pr_url_of(run: Run):
    return (run.result or {}).get("pr_url") if run.result else None


def _parse_bound(value: str, *, end: bool) -> datetime:
    """ISO date or datetime, interpreted as UTC (run timestamps are UTC —
    the SPA labels its date inputs accordingly). A date-only `to` bound is
    end-INCLUSIVE: it becomes an exclusive next-midnight upper bound."""
    try:
        if len(value) == 10:
            d = date.fromisoformat(value)
            dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            return dt + timedelta(days=1) if end else dt
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(
            400, f"invalid date {value!r} — ISO date (YYYY-MM-DD) or "
                 f"datetime, interpreted as UTC") from None


def _token_fields(run: Run, cost_inputs: CostInputs) -> dict:
    """Scalar token/cost columns for one run. cost_usd_estimated is priced
    by the CURRENT card (None when the split is missing or the model
    unmapped) — never the persisted stamp."""
    tr = run.token_report or {}
    out = {k: tr.get(k) for k in _TOKEN_SUMS}
    out["model"] = tr.get("model")
    out["cost_usd"] = tr.get("cost_usd")
    out["cost_usd_estimated"] = costing.estimate_cost_usd(tr, cost_inputs.rates)
    return out


def list_runs_response(store, cost_inputs: CostInputs, *, limit: int = 25,
                       offset: int = 0, mission_key: str | None = None,
                       pmo_ref: str | None = None,
                       created_from: str | None = None,
                       created_to: str | None = None) -> dict:
    lo = _parse_bound(created_from, end=False) if created_from else None
    hi = _parse_bound(created_to, end=True) if created_to else None

    everything = sorted(store.all(), key=lambda r: r.created_at, reverse=True)
    runs = everything
    if mission_key:
        needle = mission_key.strip().upper()
        runs = [r for r in runs if needle in r.mission_key.upper()
                or needle in r.run_id.upper()]
    if pmo_ref:
        runs = [r for r in runs if r.pmo_ref == pmo_ref]
    if lo:
        runs = [r for r in runs if r.created_at >= lo]
    if hi:
        runs = [r for r in runs if r.created_at < hi]

    # a column no run contributed to stays None (rendered "—"): summing
    # nothing to 0 would show an all-grok fleet "cache w: 0" over rows of
    # "—", and a fleet with no cost data a fabricated $0.00
    totals: dict = {k: None for k in _TOKEN_SUMS}
    totals["runtime_seconds"] = 0
    cost = cost_est = cost_eff = tok_eff = None
    for r in runs:
        if r.started_at and r.ended_at:
            totals["runtime_seconds"] += int(
                (r.ended_at - r.started_at).total_seconds())
        tr = r.token_report or {}
        for k in _TOKEN_SUMS:
            v = tr.get(k)
            if v is not None:
                totals[k] = (totals[k] or 0) + v
        # effective run total for the totals-row label: harness-reported
        # when present (grok), else the SUM of the known splits — plain
        # arithmetic on counts (claude/codex report no total), never an
        # estimate. totals.total_tokens stays reported-only.
        run_total = tr.get("total_tokens")
        if run_total is None:
            known = [tr.get(k) for k in _TOKEN_SUMS if k != "total_tokens"
                     and tr.get(k) is not None]
            run_total = sum(known) if known else None
        if run_total is not None:
            tok_eff = (tok_eff or 0) + run_total
        native = tr.get("cost_usd")
        est = costing.estimate_cost_usd(tr, cost_inputs.rates)
        eff = costing.effective_cost(native, est, cost_inputs)
        if native is not None:
            cost = (cost or 0.0) + native
        if est is not None:
            cost_est = (cost_est or 0.0) + est
        if eff is not None:
            cost_eff = (cost_eff or 0.0) + eff
    totals["cost_usd"] = round(cost, 6) if cost is not None else None
    totals["cost_usd_estimated"] = (round(cost_est, 6)
                                    if cost_est is not None else None)
    totals["cost_usd_effective"] = (round(cost_eff, 6)
                                    if cost_eff is not None else None)
    totals["total_tokens_effective"] = tok_eff

    page = []
    for r in runs[offset:offset + limit]:
        row = r.model_dump(include=_LIST_FIELDS)
        row["pr_url"] = _pr_url_of(r)
        row.update(_token_fields(r, cost_inputs))
        page.append(row)

    return {
        "total": len(runs), "offset": offset, "limit": limit, "runs": page,
        "totals": totals,
        # from ALL runs, not the filtered set — the dropdown must not
        # collapse to the currently selected option
        "pmo_refs": sorted({r.pmo_ref for r in everything}),
        "rate_card": {"rate_card_id": cost_inputs.rate_card_id,
                      "override_native": cost_inputs.override_native},
    }


def run_detail(run: Run, cost_inputs: CostInputs) -> dict:
    body = run.model_dump(include=_DETAIL_FIELDS)
    body["pr_url"] = _pr_url_of(run)
    body.update(_token_fields(run, cost_inputs))
    return body
