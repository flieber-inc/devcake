"""Runs API service (ADR-0021 part 3): token/cost scalars on list rows,
composable filters (mission_key × pmo_ref × created date range, UTC),
totals over the ENTIRE filtered set (not the visible page), read-time
estimates that follow the CURRENT rate card, and the leak guard — prompts,
results, notes, and the raw token_report dict are never serialized."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from devcake.adapters.files.run_store import RunStore
from devcake.config import CostInputs, ModelRate
from devcake.api.runs_service import (list_runs_response, run_detail,
                                      runs_csv_response)
from devcake.domain.run import Run

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

GROK_TR = {"input_tokens": 1_000_000, "cache_read_tokens": 2_000_000,
           "cache_write_tokens": None, "output_tokens": 500_000,
           "total_tokens": 3_500_000, "cost_usd_native": None,
           "cost_usd_estimated": 5.60, "rate_card_id": "builtin-v2",
           "reasoning_tokens": 20616,
           "model": "grok-4.5-build", "source": "end_event"}
CLAUDE_TR = {"input_tokens": 10_000, "cache_read_tokens": 5_000,
             "cache_write_tokens": 2_000, "output_tokens": 1_000,
             "total_tokens": None, "cost_usd_native": 0.1234,
             "model": "claude-opus-5", "source": "session_json"}


def _run(i, *, pmo_ref="alpha", created=None, tr=None, minutes=7,
         key=None, spec_prompt="SECRET PROMPT"):
    created = created or (T0 + timedelta(hours=i))
    return Run(run_id=f"A-{i}-1-EXECUTE-{'Z' * 6}", mission_key=key or f"A-{i}",
               mission_pmo_id=f"p{i}", pmo_ref=pmo_ref, mission_type="EXECUTE",
               dev_type="senior-dev", seq=1, state="finished",
               created_at=created, started_at=created,
               ended_at=created + timedelta(minutes=minutes),
               spec_prompt=spec_prompt, token_report=tr,
               result={"outcome": "executed", "summary": "SECRET SUMMARY"})


def _store(tmp_path, runs):
    store = RunStore(tmp_path / "runs")
    for r in runs:
        store.save(r)
    return store


def test_rows_carry_token_scalars_and_read_time_estimate(tmp_path):
    store = _store(tmp_path, [_run(1, tr=GROK_TR), _run(2, tr=CLAUDE_TR),
                              _run(3, tr=None)])
    out = list_runs_response(store, CostInputs(), limit=25, offset=0)
    rows = {r["mission_key"]: r for r in out["runs"]}
    grok = rows["A-1"]
    assert grok["input_tokens"] == 1_000_000
    assert grok["cache_write_tokens"] is None
    assert grok["cost_usd"] is None
    assert grok["cost_usd_estimated"] == 5.60
    assert grok["model"] == "grok-4.5-build"
    # a SUBSET of output — displayed separately (out-cell tooltip), never
    # coalesced into another column and never priced on top
    assert grok["reasoning_tokens"] == 20616
    assert rows["A-3"]["reasoning_tokens"] is None
    claude = rows["A-2"]
    assert claude["cost_usd"] == 0.1234
    # mapped since the ADR-0033 claude-opus rate row (builtin-v2):
    # 10k×$5 + 5k×$0.50 + 2k×$6.25 + 1k×$25 per M = $0.09
    assert claude["cost_usd_estimated"] == 0.09
    bare = rows["A-3"]
    assert bare["input_tokens"] is None and bare["cost_usd"] is None
    assert out["rate_card"] == {"rate_card_id": "builtin-v2",
                                "override_native": False}


def test_read_time_estimate_follows_current_card_not_the_stamp(tmp_path):
    store = _store(tmp_path, [_run(1, tr=GROK_TR)])
    doubled = CostInputs(rates=[
        ModelRate(model_prefix="grok-4.5", input_per_mtok=4.00,
                  cache_read_per_mtok=0.60, output_per_mtok=12.00)])
    out = list_runs_response(store, doubled, limit=25, offset=0)
    assert out["runs"][0]["cost_usd_estimated"] == 11.20   # not the 5.60 stamp
    assert out["rate_card"]["rate_card_id"].startswith("operator:")


def test_filters_compose_and_are_utc(tmp_path):
    runs = [_run(1, pmo_ref="alpha"), _run(2, pmo_ref="beta"),
            _run(3, pmo_ref="alpha", created=T0 + timedelta(days=2))]
    store = _store(tmp_path, runs)
    out = list_runs_response(store, CostInputs(), limit=25, offset=0,
                             pmo_ref="alpha")
    assert {r["mission_key"] for r in out["runs"]} == {"A-1", "A-3"}
    # date-only bounds are UTC calendar days; `to` is end-inclusive
    out = list_runs_response(store, CostInputs(), limit=25, offset=0,
                             pmo_ref="alpha", created_from="2026-08-01",
                             created_to="2026-08-01")
    assert {r["mission_key"] for r in out["runs"]} == {"A-1"}
    out = list_runs_response(store, CostInputs(), limit=25, offset=0,
                             created_from="2026-08-03")
    assert {r["mission_key"] for r in out["runs"]} == {"A-3"}
    # mission_key composes with pmo_ref
    out = list_runs_response(store, CostInputs(), limit=25, offset=0,
                             mission_key="a-1", pmo_ref="beta")
    assert out["runs"] == [] and out["total"] == 0


def test_invalid_dates_are_400(tmp_path):
    store = _store(tmp_path, [_run(1)])
    for bad in ("yesterday", "2026-13-40", "08/01/2026"):
        with pytest.raises(HTTPException) as e:
            list_runs_response(store, CostInputs(), limit=25, offset=0,
                               created_from=bad)
        assert e.value.status_code == 400


def test_totals_cover_the_whole_filtered_set(tmp_path):
    runs = [_run(i, tr=GROK_TR, minutes=10) for i in range(1, 4)] \
         + [_run(4, tr=CLAUDE_TR, minutes=5)]
    store = _store(tmp_path, runs)
    out = list_runs_response(store, CostInputs(), limit=2, offset=0)
    assert len(out["runs"]) == 2 and out["total"] == 4
    t = out["totals"]
    assert t["runtime_seconds"] == 3 * 600 + 300       # ALL runs, not the page
    assert t["input_tokens"] == 3 * 1_000_000 + 10_000
    assert t["cache_write_tokens"] == 2_000            # grok nulls count as 0
    assert t["cost_usd"] == 0.1234
    assert t["cost_usd_estimated"] == round(3 * 5.60 + 0.09, 6)
    # effective prefers NATIVE per run: the claude run contributes its
    # 0.1234 native, not its 0.09 estimate
    assert t["cost_usd_effective"] == round(3 * 5.60 + 0.1234, 6)
    # pmo_refs come from ALL runs so the dropdown never collapses
    assert out["pmo_refs"] == ["alpha"]


def test_effective_token_total_prefers_reported_else_sums_splits(tmp_path):
    """The totals-row label wants ONE complete magnitude: per run, the
    harness-reported total when present (grok), else the arithmetic sum of
    the known splits (claude/codex report no total). Kept separate from
    totals.total_tokens, which stays the honest sum of REPORTED totals."""
    store = _store(tmp_path, [_run(1, tr=GROK_TR), _run(2, tr=CLAUDE_TR)])
    t = list_runs_response(store, CostInputs(), limit=25, offset=0)["totals"]
    claude_split_sum = 10_000 + 5_000 + 2_000 + 1_000
    assert t["total_tokens_effective"] == 3_500_000 + claude_split_sum
    assert t["total_tokens"] == 3_500_000          # reported-only, unchanged

    bare = _store(tmp_path / "bare", [_run(1, tr=None)])
    t = list_runs_response(bare, CostInputs(), limit=25, offset=0)["totals"]
    assert t["total_tokens_effective"] is None


def test_totals_are_null_when_no_run_contributed(tmp_path):
    """A column with zero contributions across the whole filtered set is
    UNKNOWN, not zero — an all-grok history must show cache-write "—" in the
    totals row (grok has no write counter), matching the per-row cells; a
    set with no token reports at all shows no fabricated $0.00 either."""
    store = _store(tmp_path, [_run(i, tr=GROK_TR) for i in range(1, 3)])
    t = list_runs_response(store, CostInputs(), limit=25, offset=0)["totals"]
    assert t["cache_write_tokens"] is None      # every grok row is null
    assert t["input_tokens"] == 2_000_000       # contributed sums unaffected
    assert t["cost_usd"] is None                # no native cost anywhere
    assert t["cost_usd_estimated"] == round(2 * 5.60, 6)

    bare = _store(tmp_path / "bare", [_run(1, tr=None), _run(2, tr=None)])
    t = list_runs_response(bare, CostInputs(), limit=25, offset=0)["totals"]
    for k in ("input_tokens", "output_tokens", "cache_read_tokens",
              "cache_write_tokens", "total_tokens", "cost_usd",
              "cost_usd_estimated", "cost_usd_effective"):
        assert t[k] is None, k
    assert t["runtime_seconds"] == 2 * 7 * 60   # durations stay real sums


def test_totals_respect_override_native(tmp_path):
    both = dict(GROK_TR, cost_usd_native=3.0)
    store = _store(tmp_path, [_run(1, tr=both, minutes=1)])
    off = list_runs_response(store, CostInputs(), limit=25, offset=0)
    assert off["totals"]["cost_usd_effective"] == 3.0
    on = list_runs_response(store, CostInputs(override_native=True),
                            limit=25, offset=0)
    assert on["totals"]["cost_usd_effective"] == 5.60


def test_sort_orders_whole_set_nulls_always_last(tmp_path):
    """Sorting is server-side over the ENTIRE filtered set (client-side
    would only reorder the visible page) and null-valued runs sink to the
    bottom in BOTH directions — an ascending cost sort must not lead with
    forty token-less OAuth rows."""
    cheap = dict(GROK_TR, input_tokens=100_000, cache_read_tokens=100_000,
                 output_tokens=10_000, total_tokens=210_000)
    store = _store(tmp_path, [_run(1, tr=cheap, minutes=3),
                              _run(2, tr=None, minutes=99),
                              _run(3, tr=GROK_TR, minutes=1)])
    by_cost = list_runs_response(store, CostInputs(), limit=25, offset=0,
                                 sort="cost", direction="desc")
    assert [r["mission_key"] for r in by_cost["runs"]] == ["A-3", "A-1", "A-2"]
    by_cost_asc = list_runs_response(store, CostInputs(), limit=25, offset=0,
                                     sort="cost", direction="asc")
    assert [r["mission_key"] for r in by_cost_asc["runs"]] == ["A-1", "A-3", "A-2"]
    by_dur = list_runs_response(store, CostInputs(), limit=25, offset=0,
                                sort="duration", direction="desc")
    assert [r["mission_key"] for r in by_dur["runs"]] == ["A-2", "A-1", "A-3"]
    by_in = list_runs_response(store, CostInputs(), limit=25, offset=0,
                               sort="input_tokens", direction="asc")
    assert [r["mission_key"] for r in by_in["runs"]] == ["A-1", "A-3", "A-2"]


def test_sort_by_cost_respects_override_and_bad_params_400(tmp_path):
    both = dict(GROK_TR, cost_usd_native=9.0)     # native 9.0, estimate 5.6
    other = dict(GROK_TR, cost_usd_native=6.0,
                 input_tokens=2_000_000, total_tokens=4_500_000)  # est 7.6
    store = _store(tmp_path, [_run(1, tr=both), _run(2, tr=other)])
    off = list_runs_response(store, CostInputs(), limit=25, offset=0,
                             sort="cost", direction="desc")
    assert [r["mission_key"] for r in off["runs"]] == ["A-1", "A-2"]   # 9 > 6
    on = list_runs_response(store, CostInputs(override_native=True),
                            limit=25, offset=0, sort="cost", direction="desc")
    assert [r["mission_key"] for r in on["runs"]] == ["A-2", "A-1"]    # 7.6 > 5.6
    for bad in ({"sort": "verdict"}, {"sort": "cost", "direction": "sideways"},
                {"group_by": "dev_type"}):
        with pytest.raises(HTTPException) as e:
            list_runs_response(store, CostInputs(), limit=25, offset=0, **bad)
        assert e.value.status_code == 400


def test_group_by_mission_clusters_paginates_and_sorts_groups(tmp_path):
    """Grouped mode: the pagination unit becomes the MISSION, groups carry
    subtotals with the same null semantics as the grand totals, the active
    sort orders GROUPS by their aggregate, and runs inside a group stay in
    pipeline order (seq) regardless of that sort."""
    def mrun(i, key, seq, tr, pmo_ref="alpha", minutes=5):
        r = _run(i, tr=tr, minutes=minutes, key=key)
        return r.model_copy(update={"seq": seq, "pmo_ref": pmo_ref,
                                    "run_id": f"{key}-{seq}-X-{i:06d}"})

    pricey = dict(GROK_TR)                                  # est 5.6/run
    cheap = dict(GROK_TR, input_tokens=100_000, cache_read_tokens=100_000,
                 output_tokens=10_000, total_tokens=210_000)  # est ~0.29
    store = _store(tmp_path, [
        mrun(1, "A-1", 2, pricey), mrun(2, "A-1", 1, pricey),
        mrun(3, "A-2", 1, cheap),
        mrun(4, "A-1", 1, cheap, pmo_ref="beta"),   # same key, other PMO
    ])
    out = list_runs_response(store, CostInputs(), limit=25, offset=0,
                             group_by="mission", sort="cost",
                             direction="desc")
    assert out["total"] == 3 and out["total_runs"] == 4
    groups = out["groups"]
    assert [(g["pmo_ref"], g["mission_key"]) for g in groups] == [
        ("alpha", "A-1"), ("beta", "A-1"), ("alpha", "A-2")]   # by subtotal
    assert groups[0]["subtotal"]["cost_usd_effective"] == round(2 * 5.6, 6)
    assert groups[0]["subtotal"]["cache_write_tokens"] is None  # null rules
    # pipeline order inside the group, not the cost sort
    assert [r["seq"] for r in groups[0]["runs"]] == [1, 2]
    for banned in ("spec_prompt", "result", "notes", "token_report"):
        assert banned not in groups[0]["runs"][0]
    # grand totals still cover every filtered run, not the page of groups
    assert out["totals"]["cost_usd_effective"] == round(
        2 * 5.6 + 2 * 0.29, 6)
    # pagination walks GROUPS
    page = list_runs_response(store, CostInputs(), limit=2, offset=2,
                              group_by="mission", sort="cost",
                              direction="desc")
    assert [(g["pmo_ref"], g["mission_key"]) for g in page["groups"]] == [
        ("alpha", "A-2")]
    assert page["total"] == 3


def test_rows_surface_harness_version_model_and_mission_url(tmp_path):
    """Runs-table telemetry: harness + version + the model that actually
    ran + a PMO link. spec_env stays off the wire — only the two
    non-secret keys are lifted."""
    r = _run(1, tr=GROK_TR)
    r.spec_env = {"DEVCAKE_HARNESS": "grok-build", "DEVCAKE_MODEL": "grok-4.5"}
    r.harness_version = "0.2.112"
    r.mission_url = "https://linear.app/acme/issue/A-1"
    store = _store(tmp_path, [r])
    row = list_runs_response(store, CostInputs(), limit=25, offset=0)["runs"][0]
    assert row["harness"] == "grok-build"
    assert row["harness_version"] == "0.2.112"
    # token_report.model is what ran; the Dev-Type pin is only a fallback
    assert row["model"] == "grok-4.5-build"
    assert row["mission_url"] == "https://linear.app/acme/issue/A-1"
    assert "spec_env" not in row
    detail = run_detail(store.get(r.run_id), CostInputs())
    assert detail["harness"] == "grok-build"
    assert detail["harness_version"] == "0.2.112"
    assert detail["mission_url"] == "https://linear.app/acme/issue/A-1"


def test_model_falls_back_to_the_dispatch_pin_when_unreported(tmp_path):
    r = _run(1, tr=None)
    r.spec_env = {"DEVCAKE_HARNESS": "claude-code",
                  "DEVCAKE_MODEL": "claude-fable-5"}
    store = _store(tmp_path, [r])
    row = list_runs_response(store, CostInputs(), limit=25, offset=0)["runs"][0]
    assert row["harness"] == "claude-code"
    assert row["model"] == "claude-fable-5"
    assert row["harness_version"] is None
    assert row["mission_url"] is None


def test_mission_url_joins_live_cache_when_unstamped(tmp_path):
    """Pre-field records have no mission_url; the last poll snapshot fills
    the link for missions still on the board (instance-qualified)."""
    r = _run(1)
    r.pmo_ref = "linear"
    r.mission_pmo_id = "p1"
    store = _store(tmp_path, [r])
    cache = [{"instance": "linear", "pmo_id": "p1", "key": "A-1",
              "url": "https://linear.app/acme/issue/A-1"}]
    row = list_runs_response(store, CostInputs(), limit=25, offset=0,
                             missions_cache=cache)["runs"][0]
    assert row["mission_url"] == "https://linear.app/acme/issue/A-1"
    other = [{"instance": "other", "pmo_id": "p1", "key": "A-1",
              "url": "https://evil.example/x"}]
    row2 = list_runs_response(store, CostInputs(), limit=25, offset=0,
                              missions_cache=other)["runs"][0]
    assert row2["mission_url"] is None


def test_rows_and_detail_never_leak_prompts_results_or_notes(tmp_path):
    store = _store(tmp_path, [_run(1, tr=GROK_TR)])
    out = list_runs_response(store, CostInputs(), limit=25, offset=0)
    row = out["runs"][0]
    for banned in ("spec_prompt", "result", "notes", "token_report",
                   "spec_env", "auth_digest"):
        assert banned not in row
    assert "SECRET" not in str(out)
    detail = run_detail(store.get("A-1-1-EXECUTE-ZZZZZZ"), CostInputs())
    for banned in ("spec_prompt", "notes", "token_report", "spec_env",
                   "auth_digest"):
        assert banned not in detail
    assert detail["cost_usd_estimated"] == 5.60
    assert detail["input_tokens"] == 1_000_000


# ── CSV export (docs/11: the Runs ⋯ menu's spreadsheet) ──────────────────────

def _csv_rows(resp):
    import csv as _csv
    import io as _io
    return list(_csv.reader(_io.StringIO(resp.body.decode())))


def test_csv_exports_whole_filtered_set_with_header(tmp_path):
    # no pagination: 39 runs is more than one page and ALL of them export
    store = _store(tmp_path, [_run(i, tr=GROK_TR) for i in range(1, 40)])
    resp = runs_csv_response(store, CostInputs())
    rows = _csv_rows(resp)
    assert rows[0][0] == "run_id" and "cost_usd_effective" in rows[0]
    assert "harness" in rows[0] and "harness_version" in rows[0]
    assert "mission_url" in rows[0]
    assert len(rows) == 40
    assert resp.media_type == "text/csv"
    assert resp.headers["content-disposition"].startswith(
        'attachment; filename="devcake-runs-')


def test_csv_respects_filters_via_the_shared_pipe(tmp_path):
    store = _store(tmp_path, [_run(1, pmo_ref="alpha", tr=GROK_TR),
                              _run(2, pmo_ref="beta", tr=CLAUDE_TR)])
    rows = _csv_rows(runs_csv_response(store, CostInputs(), pmo_ref="beta"))
    assert len(rows) == 2
    assert "reasoning_tokens" in rows[0]
    row = dict(zip(rows[0], rows[1]))
    assert row["pmo_ref"] == "beta" and row["mission_key"] == "A-2"
    assert row["created_at"] == (T0 + timedelta(hours=2)).isoformat()


def test_csv_effective_cost_matches_the_ui_rule(tmp_path):
    # native present + override off ⇒ native; override on ⇒ current estimate
    store = _store(tmp_path, [_run(1, tr=CLAUDE_TR)])
    row = dict(zip(*_csv_rows(runs_csv_response(store, CostInputs()))[:2]))
    assert row["cost_usd_effective"] == "0.1234"
    assert row["rate_card_id"] == "builtin-v2"
    over = CostInputs(override_native=True)
    row = dict(zip(*_csv_rows(runs_csv_response(store, over))[:2]))
    assert row["cost_usd_effective"] == "0.09"


def test_csv_neutralizes_formula_cells_but_not_numbers(tmp_path):
    r = _run(1, tr=CLAUDE_TR)
    r.state, r.error = "failed", "=HYPERLINK(\"http://evil\")"
    store = _store(tmp_path, [r])
    row = dict(zip(*_csv_rows(runs_csv_response(store, CostInputs()))[:2]))
    assert row["error"] == "'=HYPERLINK(\"http://evil\")"    # inert in Excel
    assert row["cost_usd"] == "0.1234"                       # numbers stay raw


def test_csv_cell_neutralizes_every_cwe1236_lead_in():
    """Audit find: the first cut guarded only =/+/-/@ — Excel and Sheets
    also strip a leading TAB/CR/LF/space before evaluating what follows."""
    from devcake.api.runs_service import _csv_cell
    for lead in ("=", "+", "-", "@", "\t", "\r", "\n", " "):
        cell = lead + 'HYPERLINK("http://evil")'
        assert _csv_cell(cell) == "'" + cell, repr(lead)
    assert _csv_cell(-3.5) == -3.5           # negative numbers stay numeric
    assert _csv_cell("claude-fable-5") == "claude-fable-5"   # benign strings


def test_csv_sort_orders_the_whole_set_and_leaks_nothing(tmp_path):
    store = _store(tmp_path, [_run(1, tr=CLAUDE_TR), _run(2, tr=GROK_TR)])
    resp = runs_csv_response(store, CostInputs(), sort="cost",
                             direction="desc")
    rows = _csv_rows(resp)
    assert [r[2] for r in rows[1:]] == ["A-2", "A-1"]   # grok est 5.60 first
    assert "SECRET" not in resp.body.decode()
    with pytest.raises(HTTPException):
        runs_csv_response(store, CostInputs(), sort="bogus")


def test_csv_empty_store_is_header_only(tmp_path):
    rows = _csv_rows(runs_csv_response(_store(tmp_path, []), CostInputs()))
    assert len(rows) == 1
