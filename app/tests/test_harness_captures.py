"""ADR-0018 evidence — what the SHIPPED fault predicate does with real captures.

One row per capture in `fixtures/harness_streams/`, discovered from the
`*.meta.json` sidecars on disk rather than listed by hand, so a capture dropped
into that directory cannot be silently unasserted
(`test_every_sidecar_has_a_row`). Every fixture byte is verbatim CLI stdout;
nothing here is hand-written or edited.

**Measured facts and expectations must not share a file.** A sidecar holds only
what the capture rig measured — argv, exit status, byte counts, and the verdict
the predicate happened to give AT CAPTURE TIME. The `intended` verdict is a
human judgement about what SHOULD happen, and it lives in `CAPTURES` below, in
this file, under review. If an expectation could be written into a sidecar, a
capture could be "corrected" into agreement with a predicate that is wrong, and
the whole exercise would grade itself.

For the same reason a sidecar's `observed_reason` is deliberately NOT asserted
here: it is a snapshot of a predicate that is about to change. What IS asserted
against every sidecar is its byte counts (`test_sidecar_byte_counts_match_the_
committed_files`) — pure facts about the fixture, and the guard that catches a
stream and its sidecar drifting apart.

Rows whose observed verdict differs from the intended one carry
`@pytest.mark.xfail(strict=True, reason=...)` naming the mechanism that produces
the wrong answer. This module is the EVIDENCE half of a two-commit sequence: the
follow-up fix commit is finished exactly when every xfail here has been removed,
and `strict=True` is what makes that true in both directions — an accidental fix
cannot pass silently either.
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest

# host checkout: repo/app/tests → repo/images/common; app container: /srv/tests
# → /srv/images/common (read-only compose mount)
_CANDIDATES = [Path(__file__).parents[2] / "images" / "common" / "dev_entrypoint.py",
               Path(__file__).parents[1] / "images" / "common" / "dev_entrypoint.py"]
ENTRYPOINT = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

_ENV_KEYS = ("DEVCAKE_RUN_ID", "REDIS_URL", "REDIS_USER", "REDIS_PASSWORD")
_saved = {k: os.environ.get(k) for k in _ENV_KEYS}
os.environ.setdefault("DEVCAKE_RUN_ID", "T-1-1-EXECUTE-AAAAAA")
os.environ.setdefault("REDIS_URL", "redis://localhost:6399/0")
os.environ.setdefault("REDIS_USER", "test")
os.environ.setdefault("REDIS_PASSWORD", "test")

# distinct module name — the other entrypoint suites load the same file under
# their own names in this pytest process
spec = importlib.util.spec_from_file_location("dev_entrypoint_captures", ENTRYPOINT)
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)

for _k, _v in _saved.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

FIXTURES = Path(__file__).parent / "fixtures" / "harness_streams"

META = {p.name[:-len(".meta.json")]: json.loads(p.read_text())
        for p in sorted(FIXTURES.glob("*.meta.json"))}

# The 2026-07-24 Claude Code 2.1.219 incident captures predate the capture rig,
# so they carry no sidecar and are asserted by test_entrypoint_fault.py instead.
PRE_SIDECAR_STREAMS = frozenset({
    "claude_aborted_streaming", "claude_api_error_400",
    "claude_empty_completion", "claude_max_turns",
})


def companion(name: str, suffix: str) -> str:
    """A capture's side file, or "" — the rig writes one only when non-empty."""
    path = FIXTURES / f"{name}.{suffix}"
    return path.read_text() if path.exists() else ""


def harness_dump(harness: str, name: str, out: str) -> str:
    """`dump` exactly as main() computes it (dev_entrypoint ~L1427).

    Only grok's comes from outside the stream — it is the output of a separate
    `grok export <sessionId>` call — which is why grok is the only harness that
    promotes a `.dump.txt` companion. For codex and claude the dump is a pure
    function of the committed `.jsonl`, so duplicating it would just be a second
    thing to keep in sync.
    """
    if harness == "codex":
        return ep.codex_text_dump(out)
    if harness == "grok-build":
        return companion(name, "dump.txt")
    return ep.claude_text_dump(out)


def verdict(name: str) -> tuple:
    """(reason, exit code, error class) for one capture, wired as main() wires it.

    `(None, None, None)` means no fault on a clean exit: main() then hands the
    run to the result.json path, so there is no failure verdict at all.
    """
    meta = META[name]
    harness, harness_exit = meta["harness"], meta["exit_code"]
    out = (FIXTURES / f"{name}.jsonl").read_text()
    dump = harness_dump(harness, name, out)
    fault = ep.harness_fault(harness, out, harness_exit, dump=dump,
                             last_message=companion(name, "last_message.txt"))
    # main() ~L1443: api_error_status is extracted ONLY for claude, so a
    # structured 401 on codex or grok never reaches auth_evidence_is_distinctive.
    api_status = None
    if harness not in ("codex", "grok-build"):
        _ev = ep.claude_result_event(out)
        api_status = ep._dict(_ev).get("api_error_status") if _ev else None
    reason = fault["reason"] if fault else None
    if harness_exit != 0:                       # main() ~L1466: the composed rule
        return (reason,) + ep.classify_nonzero_exit(
            companion(name, "stderr.txt")[-1500:], fault, api_status)
    if fault:                                   # main() rows 6/7 and the plan gate
        return ((reason, 16, "DEV_TURN_BUDGET") if reason == ep.FAULT_TURN_BUDGET
                else (reason, 15, "DEV_HARNESS_FAULT"))
    return (None, None, None)


# ── the intended verdicts (human judgement — reviewed here, never in a sidecar) ─

NO_FAULT = (None, None, None)
EMPTY = (ep.FAULT_EMPTY_COMPLETION, 15, "DEV_HARNESS_FAULT")
TERMINAL = (ep.FAULT_TERMINAL_ERROR, 15, "DEV_HARNESS_FAULT")
AUTH = (ep.FAULT_TERMINAL_ERROR, 12, "DEV_AUTH")
BUDGET = (ep.FAULT_TURN_BUDGET, 16, "DEV_TURN_BUDGET")
CRASH = (None, 10, "DEV_CRASH")

_NO_STATUS = ("`api_error_status` is extracted only for claude in `main()`, so the in-band "
              "401 never reaches `auth_evidence_is_distinctive`")

# (capture, intended verdict, why the predicate does NOT produce it today).
# An empty third column means the row already holds — it is a baseline, not a
# finding, and the fix commit must leave it alone.
CAPTURES = [
    # ── claude-code 2.1.210 — the conservatism baseline ──────────────────────
    # The arm that must NEVER fire. A refusal is protocol-identical to a healthy
    # run (same subtype, same terminal_reason, only shorter text), which is why
    # empty_completion has to be structural.
    ("claude_healthy", NO_FAULT, ""),
    ("claude_refusal", NO_FAULT, ""),

    # ── codex-cli 0.144.4 ────────────────────────────────────────────────────
    ("codex_empty", EMPTY,
     "codex emits an `item.completed` whose item type is \"error\" (`Model metadata for "
     "stub-model not found`) BEFORE `turn.started` whenever `-m` names a model the backend "
     "does not advertise, and `codex_run_fault`'s `elif item: items += 1` scores it as tool "
     "activity — so items == 1 and `empty_completion` can never fire. Every local backend "
     "serves a model id codex has no metadata for, so in the deployment ADR-0018 was written "
     "for, a 200-with-nothing is laundered into exit 11 DEV_BAD_OUTPUT."),
    # THE controlled counterfactual for the row above: same stub scenario, same
    # CLI, argv differing only in the `-m stub-model` pair, and the arm fires.
    ("codex_empty_no_model", EMPTY, ""),
    ("codex_healthy", NO_FAULT, ""),
    ("codex_whitespace", EMPTY,
     "`messages += 1` fires on any `agent_message` item with no `.strip()`, so a "
     "whitespace-only completion is not classified — unlike `_claude_activity`, which strips. "
     "The `-o` file is four whitespace bytes and `codex_text_dump` is empty, so nothing was "
     "actually said. Two arms, two different notions of \"text\"."),
    # Tool work with no closing message is not a fault, whatever the counters say.
    ("codex_tool_only", NO_FAULT, ""),
    ("codex_refusal", NO_FAULT, ""),
    ("codex_http_400", TERMINAL, ""),
    ("codex_http_401", AUTH,
     f"{_NO_STATUS}; codex's stderr is the 39-byte `Reading additional input from stdin...` "
     "on every failure and matches no auth marker, so the predicate's terminal_error wins and "
     "`classify_nonzero_exit` returns 15. Exit 15 is correlation-eligible, so an expired key "
     "rolled out to a whole Dev Type reads as a shared backend outage and is excused instead "
     "of latching the auth breaker."),
    ("codex_http_401_retrying", AUTH,
     f"{_NO_STATUS}. Same 401 with codex's DEFAULT retry policy — five `Reconnecting... N/5` "
     "`error` events before the bare one — which proves the retry noise does not change the "
     "verdict either way."),
    ("codex_http_429", TERMINAL, ""),
    ("codex_http_500", TERMINAL, ""),
    # 404 on /v1/responses — a backend that lacks the route at all. Same family
    # as the HTTP rows above: a hard error the harness reports and cannot retry.
    ("codex_no_route", TERMINAL, ""),
    # `no_terminal_event` is UNREACHABLE for codex, so `terminal_error` is the
    # intended reason here and not a concession: every codex failure path emits a
    # plain {"type":"error"} immediately before `turn.failed`
    # (test_codex_always_emits_an_error_event_before_turn_failed), so a stream
    # with no `turn.completed` always has an error message to classify on.
    ("codex_truncated", TERMINAL, ""),
    ("codex_truncated_retrying", TERMINAL, ""),

    # ── grok-build 0.2.112 ───────────────────────────────────────────────────
    ("grok_healthy", NO_FAULT, ""),
    ("grok_refusal", NO_FAULT, ""),
    # 16 real tool executions, and the stream is ONE line long (the `end` event).
    # Conservative only because the `grok export` transcript lists the tool calls.
    ("grok_tool_only", NO_FAULT, ""),
    # Right answer, wrong evidence — and a CONSTRAINT ON THE FIX: the arm fires
    # only because the `{"type":"error"}` event carries no `sessionId`, so `dump`
    # is empty. grok names the condition itself (`no_visible_content`), so a new
    # error-event arm must not swallow this row into terminal_error.
    ("grok_empty", EMPTY, ""),
    ("grok_whitespace", EMPTY,
     "`grok export` prints Markdown beginning with `## User` + the prompt, so `dump` is never "
     "empty when a session id exists. `empty_completion` is INVERTED for grok: it fires only "
     "when grok crashed hard enough to have no session id, and never in the 200-with-nothing "
     "case it was written for."),
    ("grok_http_401", AUTH,
     "no `terminal_error` arm exists for grok (`GROK_FAULT_STOP_REASONS` is empty by design "
     "and there is no `error`-event arm), so a 401 falls through to `empty_completion`; and "
     f"{_NO_STATUS}. grok's stderr DOES match the generic `unauthorized` marker, which is how "
     "this exited 12 DEV_AUTH before ADR-0018 — the generic arm now ranks below the predicate, "
     "so the one row where ADR-0018 changes a grok outcome, it changes it for the worse."),
    ("grok_http_429", TERMINAL,
     "no `terminal_error` arm exists for grok (`GROK_FAULT_STOP_REASONS` is empty by design "
     "and there is no `error`-event arm), so every hard backend failure lands in "
     "`empty_completion` — and only because the `{\"type\":\"error\"}` event carries no "
     "`sessionId`, leaving `dump` empty. Same exit, wrong reason in `error_detail`."),
    ("grok_http_500", TERMINAL,
     "no `terminal_error` arm exists for grok, so the HTTP 500 lands in `empty_completion` "
     "(again only because the error event carries no `sessionId`). Same exit, wrong reason."),
    ("grok_truncated", TERMINAL,
     "grok always emits a terminal `{\"type\":\"error\"}` event first — the truncation is "
     "reported as `reqwest error stream: Transport error` — so `no_terminal_event` is "
     "unreachable for grok and `terminal_error` is the intended reason. There is no "
     "`terminal_error` arm either, so it lands in `empty_completion`."),
    ("grok_turn_budget", BUDGET,
     "no `turn_budget` arm exists in `grok_run_fault`, although grok announces the cap twice "
     "in band — a `{\"type\":\"max_turns_reached\"}` event and `end` with "
     "`stopReason:\"Cancelled\"` — plus `Error: max turns reached` on stderr. With no fault "
     "and no auth marker the run falls through to 10 DEV_CRASH, so a deterministic cap is "
     "charged to the Dev as a crash and retried."),
    # JUDGEMENT CALL, flagged: the task guidance is silent on this row. grok
    # 0.2.112 exits 2 at argument parsing when EXTRA_ARGS adds a second
    # --output-format, so the CLI never ran and stdout is zero bytes. Calling
    # that a harness fault makes an operator misconfiguration correlation-
    # eligible and deterministic-across-the-fleet — exactly what turn_budget is
    # separated out to avoid — while stderr carries the precise clap diagnosis
    # that exit 10 surfaces. Pre-ADR-0018 it was 10.
    ("grok_json_blob", CRASH,
     "`grok_run_fault` reads a zero-byte stream as `no_terminal_event`, so a CLI that refused "
     "its own argv is reported as a shared-backend-shaped harness fault (15, correlation-"
     "eligible) instead of the crash it is. ADR-0018 regressed this row from 10 the same way "
     "it regressed grok's and codex's 401s."),
]


def _param(name: str, intended: tuple, xfail: str):
    marks = [pytest.mark.xfail(strict=True, reason=xfail)] if xfail else []
    return pytest.param(name, intended, id=name, marks=marks)


@pytest.mark.parametrize("name,intended", [_param(*row) for row in CAPTURES])
def test_capture_gets_the_intended_verdict(name, intended):
    """The whole table: run the working-tree predicate over the captured bytes."""
    assert verdict(name) == intended


# ── the capture set is closed ────────────────────────────────────────────────

def test_every_sidecar_has_a_row():
    """Discovery is from disk, so a capture added to the fixture directory
    without a judgement in CAPTURES fails here rather than going unasserted."""
    assert set(META) == {row[0] for row in CAPTURES}


def test_every_stream_is_either_sidecarred_or_a_pre_rig_incident_capture():
    streams = {p.name[:-len(".jsonl")] for p in FIXTURES.glob("*.jsonl")}
    assert streams - set(META) == set(PRE_SIDECAR_STREAMS)


@pytest.mark.parametrize("name", sorted(META))
def test_sidecar_byte_counts_match_the_committed_files(name):
    """Drift guard. A sidecar records only measured facts, so every count in it
    must still be recomputable from the committed bytes; if one is not, the
    stream and its sidecar came from different runs and nothing else here means
    anything."""
    meta = META[name]
    out = (FIXTURES / f"{name}.jsonl").read_text()
    assert (len(out), len(out.splitlines())) == (meta["stdout_bytes"], meta["stdout_lines"])
    assert len(companion(name, "stderr.txt")) == meta["stderr_bytes"]
    assert len(companion(name, "last_message.txt")) == meta["last_message_bytes"]
    assert len(harness_dump(meta["harness"], name, out)) == meta["dump_bytes"]


# ── the mechanisms, measured ─────────────────────────────────────────────────
# Facts about the captured bytes, not about the predicate — they stay true after
# the fix commit, and they are what the xfail reasons above assert in prose.

def events(name: str) -> list:
    out = (FIXTURES / f"{name}.jsonl").read_text()
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_the_two_codex_empty_captures_differ_only_in_the_model_pin():
    """The controlled counterfactual behind `codex_empty`: same stub scenario,
    same CLI, and the ONLY argv difference is `-m stub-model`."""
    pinned, bare = META["codex_empty"]["argv"], META["codex_empty_no_model"]["argv"]
    assert [a for a in pinned if a not in bare] == ["-m", "stub-model"]
    assert [a for a in bare if a not in pinned] == []
    assert "/s/empty/v1" in " ".join(pinned) and "/s/empty/v1" in " ".join(bare)


def test_the_model_pin_adds_an_error_item_that_scores_as_tool_activity():
    """`Model metadata … not found` arrives as an `item.completed` before
    `turn.started`, and its item type is "error" — not `agent_message` — so
    codex_run_fault's `elif item: items += 1` counts it as a tool call."""
    kinds = [e.get("type") for e in events("codex_empty")]
    items = [e["item"] for e in events("codex_empty") if e.get("type") == "item.completed"]
    assert kinds.index("item.completed") < kinds.index("turn.started")
    assert [i.get("item_type") or i.get("type") for i in items] == ["error"]
    assert "Model metadata" in items[0]["message"]
    # the counterfactual carries no item at all, and both completed a turn
    assert [e.get("type") for e in events("codex_empty_no_model")] == [
        "thread.started", "turn.started", "turn.completed"]
    for name in ("codex_empty", "codex_empty_no_model"):
        done = [e for e in events(name) if e.get("type") == "turn.completed"]
        assert done and done[0]["usage"]["output_tokens"] == 0


def test_codex_always_emits_an_error_event_before_turn_failed():
    """Why `no_terminal_event` is unreachable for codex — and why the predicate's
    `turn.*` guard never contributes: `error_msg` is already set when it runs."""
    failures = [n for n, m in META.items()
                if m["harness"] == "codex" and m["exit_code"] != 0]
    assert len(failures) == 8
    for name in failures:
        kinds = [e.get("type") for e in events(name)]
        assert "turn.completed" not in kinds
        assert kinds.index("error") < kinds.index("turn.failed")


def test_codex_reports_a_401_in_band_only():
    """The evidence for the intended 12: the status is in the stream, and the
    only channel the pre-ADR-0018 classifier read says nothing about it."""
    stream = (FIXTURES / "codex_http_401.jsonl").read_text()
    stderr = companion("codex_http_401", "stderr.txt")
    assert "401 Unauthorized" in stream
    assert "401" not in stderr and stderr.strip() == "Reading additional input from stdin..."
    assert ep.classify_harness_failure(stderr) == 10
    assert ep.auth_evidence_is_distinctive(stderr) is False


def test_grok_export_echoes_the_prompt_so_dump_is_never_empty():
    """The finding that inverts grok's `empty_completion`: the whole transcript
    of a run that produced nothing useful is the prompt read back."""
    dump = companion("grok_whitespace", "dump.txt")
    assert dump.startswith("## User") and "ACKNOWLEDGED" in dump
    text, session = ep.grok_stream_parse((FIXTURES / "grok_whitespace.jsonl").read_text())
    assert text.strip() == "" and session
    # and the arm therefore fires only where there is no session to export
    for name in ("grok_empty", "grok_http_401", "grok_http_429", "grok_truncated"):
        assert META[name]["session_id"] is None
        assert all("sessionId" not in e for e in events(name))


def test_grok_reports_every_hard_failure_as_one_terminal_error_event():
    """Why `terminal_error` is the intended reason for grok's failure rows, and
    why `no_terminal_event` is unreachable for it."""
    for name in ("grok_empty", "grok_http_401", "grok_http_429", "grok_http_500",
                 "grok_truncated"):
        assert [e.get("type") for e in events(name)] == ["error"]
        assert events(name)[0]["message"]


def test_grok_announces_the_turn_cap_twice_in_band():
    """A `turn_budget` arm is implementable for grok — the capture says so."""
    assert [e.get("type") for e in events("grok_turn_budget")] == [
        "max_turns_reached", "end"]
    assert events("grok_turn_budget")[1]["stopReason"] == "Cancelled"
    assert companion("grok_turn_budget", "stderr.txt").strip() == "Error: max turns reached"


def test_grok_401_stderr_is_the_generic_marker_that_used_to_reach_exit_12():
    """The regression in one assertion: the wording is auth wording, but only the
    GENERIC kind, and the generic arm now ranks below the predicate."""
    stderr = companion("grok_http_401", "stderr.txt")
    assert "Unauthorized (401)" in stderr
    assert ep.classify_harness_failure(stderr) == 12
    assert ep.auth_evidence_is_distinctive(stderr) is False
    assert META["grok_http_401"]["devcake_exit_before_adr0018"] == 12


def test_grok_json_blob_never_ran_the_model():
    """The row where the CLI refused its own argv: zero bytes of stdout, exit 2,
    and a clap usage error naming the duplicated flag."""
    assert (FIXTURES / "grok_json_blob.jsonl").read_text() == ""
    assert META["grok_json_blob"]["exit_code"] == 2
    assert "--output-format" in companion("grok_json_blob", "stderr.txt")
    assert "cannot be used multiple times" in companion("grok_json_blob", "stderr.txt")
