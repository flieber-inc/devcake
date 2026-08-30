"""ADR-0029 — TokenReport v1: one CLOSED shape from every extractor.

Two layers:

1. Shape conformance — every harness capture in fixtures/harness_streams/
   runs through its real extractor and yields EXACTLY the closed key set
   with scalar-typed values (None = unknown, never an absent key). The
   provenance enum is pinned. This is what lets app consumers read fixed
   keys with zero per-harness branches (evaluation F22).
2. SQL-projectability — the doctrine sentence made executable: every
   persisted record keeps a stable, scalar-typed column set. The Run model
   and the TokenReport keys are pinned as would-be DDL, so widening either
   is a DELIBERATE diff, and non-scalar fields stay confined to the
   explicit blob allowlist. NO database exists — ADR-0002 designates the
   StatePort adapter swap as the exit, and these pins are what make that
   swap mechanical.
"""

import importlib.util
import json
import os
import sys
import typing
from pathlib import Path

import pytest

from devcake.domain.run import Run

_ROOTS = [Path(__file__).parents[2], Path(__file__).parents[1]]
IMAGES_COMMON = next(
    (r / "images" / "common" for r in _ROOTS
     if (r / "images" / "common" / "devcake_dev").is_dir()),
    _ROOTS[0] / "images" / "common")
if str(IMAGES_COMMON) not in sys.path:
    sys.path.insert(0, str(IMAGES_COMMON))

from devcake_dev.harness import tokens  # noqa: E402
from devcake_dev.harness.continuation import merge_token_reports  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "harness_streams"

CLOSED_KEYS = set(tokens.TOKEN_REPORT_KEYS)
SCALAR_KEYS = CLOSED_KEYS - {"raw"}


def stream(name: str) -> str:
    return (FIXTURES / f"{name}.jsonl").read_text()


def _assert_v1(report: dict, context: str):
    assert set(report) == CLOSED_KEYS, (
        f"{context}: keys {sorted(set(report) ^ CLOSED_KEYS)} out of shape")
    assert report["schema"] == tokens.TOKEN_REPORT_SCHEMA
    assert report["source"] in tokens.TOKEN_REPORT_SOURCES
    assert isinstance(report["raw"], dict)
    for key in SCALAR_KEYS - {"schema", "source"}:
        v = report[key]
        assert v is None or isinstance(v, (int, float, str)), (
            f"{context}: {key} is {type(v).__name__} — scalars only")
    # the app-side stamp slot ships empty from the image (ADR-0021)
    assert report["cost_usd_estimated"] is None


def _report_for(name: str):
    """Run the fixture through ITS extractor, exactly as harness_main does —
    including the unavailable fallback when the stream carries nothing."""
    out = stream(name)
    if name.startswith("codex"):
        return tokens.codex_token_report(out) or tokens.unavailable_report(
            model="codex")
    if name.startswith("pi_"):
        return tokens.pi_token_report(out) or tokens.unavailable_report(
            model="pi")
    if name.startswith("opencode_"):
        return tokens.opencode_token_report(out) or tokens.unavailable_report(
            model="opencode")
    if name.startswith("qwen_"):
        from devcake_dev.harness.tokens import qwen_result_event
        ev = qwen_result_event(out)
        return (tokens.qwen_token_report(ev)
                if ev is not None
                else tokens.unavailable_report(model="qwen-code"))
    if name.startswith("grok"):
        parsed = tokens.grok_stream_parse(out)
        terminal = tokens.grok_end_event(out) if parsed is not None else None
        return (tokens.grok_end_report(terminal)
                or tokens.unavailable_report(model="grok-build"))
    from devcake_dev.domain.fault import claude_result_event
    ev = claude_result_event(out)
    if ev is None:
        try:
            ev = json.loads(out)
        except Exception:
            return tokens.unavailable_report(model="claude-code")
    return tokens.claude_token_report(ev)


ALL_CAPTURES = sorted(p.stem for p in FIXTURES.glob("*.jsonl"))


def test_the_capture_battery_exists():
    assert len(ALL_CAPTURES) >= 20  # the rig's committed sweep, all harnesses


@pytest.mark.parametrize("name", ALL_CAPTURES)
def test_every_capture_yields_the_closed_shape(name):
    _assert_v1(_report_for(name), name)


def test_grok_end_event_report_is_v1_with_provenance():
    r = _report_for("grok_healthy")
    assert r["source"] == "end_event"
    assert r["reasoning_tokens"] == 0          # first-class, not a notes regex
    assert r["cost_usd_native"] is None        # grok reports no cost — never 0
    assert r["raw"]["usage"]["input_tokens"] == r["input_tokens"]


def test_claude_thinking_tokens_land_in_the_reasoning_slot():
    """NEW at claude-code 2.1.229 (capture-verified): thinking tokens ride
    usage.output_tokens_details — a subset of output_tokens absorbed into the
    first-class v1 reasoning slot like grok/codex; a pre-2.1.229 stream
    without the key stays None (never a fabricated 0)."""
    r = _report_for("claude_healthy")
    # fixture LITERAL, not extractor-vs-its-own-raw (audit nit): the
    # claude_healthy capture carries thinking_tokens: 0, and 0-read-from-
    # the-key vs None-no-key is exactly the distinction that matters
    assert r["reasoning_tokens"] == 0
    old = tokens.claude_token_report({"usage": {"output_tokens": 5}})
    assert old["reasoning_tokens"] is None


def test_signals_fallback_names_itself(tmp_path):
    """Pre-v1 the signals path masqueraded as "session_json" — provenance is
    data now, so the fallback names its actual path."""
    d = tmp_path / ".grok" / "sessions" / "w" / "sid-1"
    d.mkdir(parents=True)
    (d / "signals.json").write_text(json.dumps(
        {"contextTokensUsed": 42, "turnCount": 3, "modelsUsed": ["grok-4"]}))
    report = tokens.grok_signals_report("sid-1", home=tmp_path)
    _assert_v1(report, "signals")
    assert report["source"] == "signals"
    assert report["total_tokens"] == 42 and report["input_tokens"] is None


def test_merged_reports_stay_v1():
    a = _report_for("grok_healthy")
    _assert_v1(merge_token_reports([a, dict(a)], ["initial", "fresh"],
                                   resume_cumulative=False), "summed merge")
    cum = merge_token_reports([a, dict(a)], ["initial", "resume"],
                              resume_cumulative=True)
    _assert_v1(cum, "cumulative merge")
    assert cum["source"] == "cumulative"


def test_unavailable_is_explicit_not_silent():
    """INV-5: a run with nothing measured still reports, in full shape."""
    _assert_v1(tokens.unavailable_report(model="codex"), "unavailable")
    assert tokens.unavailable_report()["source"] == "unavailable"


# ── SQL-projectability (the ADR-0029 doctrine pins) ──────────────────────────

# The would-be DDL for `runs`: every field and whether it projects to a plain
# SQL column. Blobs are the EXPLICIT exceptions (JSON columns in a future DB);
# everything else must stay scalar-typed. Widening either list is a deliberate,
# reviewed diff — that is the point.
RUN_SCALAR_COLUMNS = {
    # rev: the lost-update fence counter (2026-08-12 audit F8) — deliberate
    # widening per this guard's own instruction; INTEGER DEFAULT 0 in DDL
    "rev",
    "schema_version", "run_id", "mission_key", "mission_pmo_id", "pmo_kind",
    "pmo_ref", "repo_ref", "mission_type", "dev_type", "seq",
    "attempt_of_step", "stage_label_at_dispatch", "branch", "spec_prompt",
    # steward_duty: ADR-0033 flavor discriminator — deliberate widening
    "steward_duty",
    # CAKE-167: compact steward finalize line for Runs-tab hover
    "outcome_summary",
    "spec_skills_dir", "state", "created_at", "started_at", "ended_at",
    "last_heartbeat", "timeout_seconds", "traceparent", "auth_digest",
    "artifact_bytes", "error", "error_class", "attempt_counted", "verdict",
    "continuations_used", "store_gen",
    # #165 — provision stamps harness --version; dispatch snapshots mission.url
    "harness_version", "mission_url",
}
RUN_BLOB_COLUMNS = {
    "blocker_work", "mirror_repos", "spec_skills", "spec_env",
    "finalized_steps", "result", "token_report",
    # ADR-0033 — the discovery steward's dispatch snapshot of the batches
    # its package carried: [{pmo_id, key, step}]; deliberate widening
    "steward_batches",
    # ADR-0031 — {entry_id, ts} reading receipt; a two-key JSON column
    "feed_watermark",
    # ADR-0016 addendum — {card: sha} supply-chain provenance for external
    # `<card>/<skill>` skills consumed by this run; deliberate widening
    "skill_repo_heads",
    # PLAN_MEMORY §3.6 — consumer memory mount provenance snapshot
    "memory_mounts",
}

_SCALAR_TYPES = (str, int, float, bool)


def _is_scalar_annotation(annotation) -> bool:
    origin = typing.get_origin(annotation)
    if origin is None:
        import datetime
        return (annotation in _SCALAR_TYPES
                or annotation is datetime.datetime
                or isinstance(annotation, type)
                and issubclass(annotation, _SCALAR_TYPES))
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if origin is typing.Union:
        return all(_is_scalar_annotation(a) for a in args)
    if origin is typing.Literal:
        return True
    return False


def test_run_model_projects_to_the_pinned_columns():
    fields = set(Run.model_fields)
    assert fields == RUN_SCALAR_COLUMNS | RUN_BLOB_COLUMNS, (
        f"Run model drifted: {sorted(fields ^ (RUN_SCALAR_COLUMNS | RUN_BLOB_COLUMNS))}"
        " — widen the pin DELIBERATELY (it is the future DDL)")
    for name in RUN_SCALAR_COLUMNS:
        assert _is_scalar_annotation(Run.model_fields[name].annotation), (
            f"Run.{name} is no longer scalar-typed — move it to the blob "
            "allowlist only if it truly cannot be a column")


def test_token_report_keys_are_the_pinned_columns():
    """The token_report blob's OWN columns: closed v1 keys + the two
    app-side stamp keys (ADR-0021). `raw` is the one nested field."""
    assert CLOSED_KEYS == {
        "schema", "model", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_write_tokens", "total_tokens",
        "reasoning_tokens", "num_turns", "duration_ms", "cost_usd_native",
        "cost_usd_estimated", "source", "raw"}
    # enum pinned: a new provenance value is a schema decision, not a typo
    assert set(tokens.TOKEN_REPORT_SOURCES) == {
        "session_json", "end_event", "signals", "cumulative", "mixed",
        "unavailable"}
