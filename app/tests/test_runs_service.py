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
from devcake.api.runs_service import list_runs_response, run_detail
from devcake.domain.run import Run

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

GROK_TR = {"input_tokens": 1_000_000, "cache_read_tokens": 2_000_000,
           "cache_write_tokens": None, "output_tokens": 500_000,
           "total_tokens": 3_500_000, "cost_usd": None,
           "cost_usd_estimated": 5.60, "rate_card_id": "builtin-v1",
           "model": "grok-4.5-build", "extraction_method": "end_event",
           "notes": "reasoning_tokens=20616"}
CLAUDE_TR = {"input_tokens": 10_000, "cache_read_tokens": 5_000,
             "cache_write_tokens": 2_000, "output_tokens": 1_000,
             "total_tokens": None, "cost_usd": 0.1234,
             "model": "claude-opus-5", "extraction_method": "session_json"}


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
    claude = rows["A-2"]
    assert claude["cost_usd"] == 0.1234
    assert claude["cost_usd_estimated"] is None        # unmapped model
    bare = rows["A-3"]
    assert bare["input_tokens"] is None and bare["cost_usd"] is None
    assert out["rate_card"] == {"rate_card_id": "builtin-v1",
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
    assert t["cost_usd_estimated"] == round(3 * 5.60, 6)
    assert t["cost_usd_effective"] == round(3 * 5.60 + 0.1234, 6)
    # pmo_refs come from ALL runs so the dropdown never collapses
    assert out["pmo_refs"] == ["alpha"]


def test_totals_respect_override_native(tmp_path):
    both = dict(GROK_TR, cost_usd=3.0)
    store = _store(tmp_path, [_run(1, tr=both, minutes=1)])
    off = list_runs_response(store, CostInputs(), limit=25, offset=0)
    assert off["totals"]["cost_usd_effective"] == 3.0
    on = list_runs_response(store, CostInputs(override_native=True),
                            limit=25, offset=0)
    assert on["totals"]["cost_usd_effective"] == 5.60


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
