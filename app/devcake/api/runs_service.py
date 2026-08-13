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

import csv
import io
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException
from fastapi.responses import PlainTextResponse

from ..config import CostInputs
from ..domain import costing
from ..domain.run import Run

_TOKEN_SUMS = ("input_tokens", "output_tokens", "cache_read_tokens",
               "cache_write_tokens", "total_tokens")

_LIST_FIELDS = {"run_id", "mission_key", "mission_type", "dev_type", "seq",
                "state", "created_at", "started_at", "ended_at", "error",
                "error_class", "attempt_counted", "verdict",
                "continuations_used"}

_DETAIL_FIELDS = _LIST_FIELDS | {
    "schema_version", "mission_pmo_id", "pmo_kind", "pmo_ref", "repo_ref",
    "attempt_of_step", "stage_label_at_dispatch", "last_heartbeat",
    "timeout_seconds", "finalized_steps", "artifact_bytes",
    "feed_watermark"}


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
    # a SUBSET of output_tokens (grok reasoning_tokens / codex
    # reasoning_output_tokens / claude thinking_tokens at 2.1.229+) —
    # informational provenance, never priced on top and deliberately NOT
    # coalesced into any other column (cache writes are input-side)
    out["reasoning_tokens"] = tr.get("reasoning_tokens")
    out["model"] = tr.get("model")
    # the API row key stays `cost_usd` (SPA contract); the stored v1 key
    # names its provenance (ADR-0029)
    out["cost_usd"] = tr.get("cost_usd_native")
    out["cost_usd_estimated"] = costing.estimate_cost_usd(tr, cost_inputs.rates)
    return out


def _accumulate_totals(runs: list[Run], cost_inputs: CostInputs) -> dict:
    """Aggregate one set of runs (the grand totals row, or one mission
    group's subtotal). A column no run contributed to stays None (rendered
    "—"): summing nothing to 0 would show an all-grok fleet "cache w: 0"
    over rows of "—", and a fleet with no cost data a fabricated $0.00."""
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
        native = tr.get("cost_usd_native")
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
    return totals


# Sortable columns (docs/11): server-side over the WHOLE filtered set —
# client-side sorting would only reorder the visible page and silently lie
# about "most expensive". "started" keeps the default dispatch order
# (created_at — started_at is null on not-yet-started runs, which must not
# sink the newest activity to the bottom).
_SORTABLE = {"started", "duration", "input_tokens", "output_tokens",
             "cache_read_tokens", "cache_write_tokens", "cost"}


def _run_sort_value(r: Run, key: str, cost_inputs: CostInputs, now: datetime):
    if key == "started":
        return r.created_at
    if key == "duration":
        if r.started_at is None:
            return None
        return ((r.ended_at or now) - r.started_at).total_seconds()
    tr = r.token_report or {}
    if key == "cost":
        return costing.effective_cost(
            tr.get("cost_usd_native"),
            costing.estimate_cost_usd(tr, cost_inputs.rates), cost_inputs)
    return tr.get(key)


def _group_sort_value(subtotal: dict, latest: datetime, key: str):
    if key == "started":
        return latest                      # most recent activity in the group
    if key == "duration":
        return subtotal["runtime_seconds"]
    if key == "cost":
        return subtotal["cost_usd_effective"]
    return subtotal[key]


def _order(items: list, value_of, descending: bool) -> list:
    """Nulls last in BOTH directions — an ascending cost sort must not lead
    with a wall of token-less rows."""
    valued = [(value_of(x), x) for x in items]
    ranked = [t for t in valued if t[0] is not None]
    ranked.sort(key=lambda t: t[0], reverse=descending)
    return [x for _, x in ranked] + [x for v, x in valued if v is None]


def _row(r: Run, cost_inputs: CostInputs) -> dict:
    row = r.model_dump(include=_LIST_FIELDS)
    row["pr_url"] = _pr_url_of(r)
    row.update(_token_fields(r, cost_inputs))
    return row


def _filtered_runs(store, cost_inputs: CostInputs, *,
                   mission_key: str | None, pmo_ref: str | None,
                   created_from: str | None, created_to: str | None,
                   sort: str | None, direction: str | None,
                   flat_sort: bool) -> tuple[list[Run], list[Run], bool]:
    """Param validation + the ONE filter pipe + (flat_sort) the flat
    ordering — shared by the JSON list and the CSV export so the export can
    never disagree with the page it was clicked from. Returns
    (filtered runs, everything, descending)."""
    if sort is not None and sort not in _SORTABLE:
        raise HTTPException(400, f"invalid sort {sort!r} — one of "
                                 f"{sorted(_SORTABLE)}")
    if direction not in (None, "asc", "desc"):
        raise HTTPException(400, f"invalid dir {direction!r} — asc or desc")
    descending = direction != "asc"
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
    if flat_sort and sort is not None:
        now = datetime.now(timezone.utc)
        runs = _order(runs, lambda r: _run_sort_value(
            r, sort, cost_inputs, now), descending)
    return runs, everything, descending


def list_runs_response(store, cost_inputs: CostInputs, *, limit: int = 25,
                       offset: int = 0, mission_key: str | None = None,
                       pmo_ref: str | None = None,
                       created_from: str | None = None,
                       created_to: str | None = None,
                       sort: str | None = None,
                       direction: str | None = None,
                       group_by: str | None = None) -> dict:
    if group_by not in (None, "mission"):
        raise HTTPException(400, f"invalid group_by {group_by!r} — mission")
    runs, everything, descending = _filtered_runs(
        store, cost_inputs, mission_key=mission_key, pmo_ref=pmo_ref,
        created_from=created_from, created_to=created_to, sort=sort,
        direction=direction, flat_sort=group_by is None)

    out = {
        "offset": offset, "limit": limit,
        "totals": _accumulate_totals(runs, cost_inputs),
        "total_runs": len(runs),
        # from ALL runs, not the filtered set — the dropdown must not
        # collapse to the currently selected option
        "pmo_refs": sorted({r.pmo_ref for r in everything}),
        "rate_card": {"rate_card_id": cost_inputs.rate_card_id,
                      "override_native": cost_inputs.override_native},
    }

    if group_by is None:
        out["total"] = len(runs)
        out["runs"] = [_row(r, cost_inputs) for r in runs[offset:offset + limit]]
        return out

    # grouped mode: the pagination unit becomes the MISSION. Group key is
    # (pmo_ref, mission_key) — mission keys are team-scoped, two PMOs may
    # both have a DEV-1 that must not merge. The active sort orders GROUPS
    # by their aggregate; runs inside a group stay in pipeline order.
    clusters: dict[tuple[str, str], list[Run]] = {}
    for r in runs:
        clusters.setdefault((r.pmo_ref, r.mission_key), []).append(r)
    groups = []
    for (ref, key), members in clusters.items():
        groups.append({
            "pmo_ref": ref, "mission_key": key, "run_count": len(members),
            "subtotal": _accumulate_totals(members, cost_inputs),
            "_members": members,
            "_latest": max(m.created_at for m in members),
        })
    groups = _order(groups, lambda g: _group_sort_value(
        g["subtotal"], g["_latest"], sort or "started"), descending)
    out["total"] = len(groups)
    page = []
    for g in groups[offset:offset + limit]:
        members = sorted(g.pop("_members"),
                         key=lambda m: (m.seq, m.attempt_of_step, m.created_at))
        g.pop("_latest")
        page.append({**g, "runs": [_row(m, cost_inputs) for m in members]})
    out["groups"] = page
    return out


def run_detail(run: Run, cost_inputs: CostInputs) -> dict:
    body = run.model_dump(include=_DETAIL_FIELDS)
    body["pr_url"] = _pr_url_of(run)
    body.update(_token_fields(run, cost_inputs))
    return body


# CSV export (docs/11): the WHOLE filtered set, flat regardless of the page's
# grouped view (grouping is a view concern). Column order is the spreadsheet
# contract; effective cost applies the same override_native rule the UI shows.
_CSV_COLUMNS = ("run_id", "pmo_ref", "mission_key", "mission_type", "dev_type",
                "seq", "state", "created_at", "started_at", "ended_at",
                "error", "error_class", "attempt_counted", "verdict",
                "continuations_used", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens", "total_tokens",
                "reasoning_tokens", "model", "cost_usd", "cost_usd_estimated",
                "cost_usd_effective", "rate_card_id", "pr_url")


def _csv_cell(v):
    """Spreadsheet-faithful cell. Strings leading with =/+/-/@ get a `'`
    prefix — error text and model ids are attacker-influenced, and csv
    quoting alone does not stop Excel/Sheets from executing a formula cell.
    The isinstance(str) guard keeps negative numbers numeric."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def runs_csv_response(store, cost_inputs: CostInputs, *,
                      mission_key: str | None = None,
                      pmo_ref: str | None = None,
                      created_from: str | None = None,
                      created_to: str | None = None,
                      sort: str | None = None,
                      direction: str | None = None) -> PlainTextResponse:
    runs, _everything, _descending = _filtered_runs(
        store, cost_inputs, mission_key=mission_key, pmo_ref=pmo_ref,
        created_from=created_from, created_to=created_to, sort=sort,
        direction=direction, flat_sort=True)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)
    for r in runs:
        row = _row(r, cost_inputs)
        row["pmo_ref"] = r.pmo_ref
        row["cost_usd_effective"] = costing.effective_cost(
            row["cost_usd"], row["cost_usd_estimated"], cost_inputs)
        row["rate_card_id"] = cost_inputs.rate_card_id
        writer.writerow([_csv_cell(row.get(c)) for c in _CSV_COLUMNS])
    name = datetime.now(timezone.utc).strftime("devcake-runs-%Y%m%d-%H%M.csv")
    return PlainTextResponse(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})
