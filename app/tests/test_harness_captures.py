"""ADR-0018 regression — fault predicate over real harness CLI streams.

Each row is a **scenario capture**: the real CLI from a baked Dev image was run
with its base URL pointed at `scripts/harness_capture/stub_backend.py` (a fake
model server that returns one named condition). Stdout is committed
byte-for-byte under `fixtures/harness_streams/`. Nothing here is hand-authored
JSON.

Sidecars (`*.meta.json`) hold measured facts only (argv, exit code, sizes).
Intended verdicts live in `CAPTURES` below — never in the sidecar — so an
expectation cannot be "corrected" without review. Discovery is from disk
(`test_every_sidecar_has_a_row`): a new capture without a CAPTURES row fails
collection.

Verdict changes are operator-visible: exit 15 can excuse and throttle a Dev
Type, 12 latches auth, 16 is always counted.
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

# Pre-rig claude streams (no sidecar) — asserted by test_entrypoint_fault.py.
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


def capture_prompt(meta: dict) -> str:
    """The prompt the rig actually sent, read back out of the recorded argv.

    It is argv[2] for all three harnesses (`claude -p P`, `grok -p P`,
    `codex exec P`) because the rig builds argv through the entrypoint's own
    `harness_argv` — except codex's resume leg (`codex exec resume SID P`,
    ADR-0022), where the prompt is argv[4]. grok's `empty_completion` arm
    needs it: the `grok export` transcript opens by echoing the prompt, and
    the prompt is what separates that echo from anything the run itself
    produced.
    """
    argv = meta["argv"]
    return argv[4] if argv[1:3] == ["exec", "resume"] else argv[2]


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
                             last_message=companion(name, "last_message.txt"),
                             prompt=capture_prompt(meta))
    # main() ~L1443: one status extractor for every harness — claude reads the
    # structured `api_error_status`, codex and grok are matched on their own
    # HTTP-layer wording, since neither exposes a field at all.
    api_status = ep.harness_api_error_status(harness, out)
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

# (capture, intended verdict). Comments record WHY a row is what it is, and in
# particular which predicate arm each one keeps honest.
CAPTURES = [
    # ── claude-code 2.1.210 — the conservatism baseline ──────────────────────
    # The arm that must NEVER fire. A refusal is protocol-identical to a healthy
    # run (same subtype, same terminal_reason, only shorter text), which is why
    # empty_completion has to be structural.
    ("claude_healthy", NO_FAULT),
    ("claude_refusal", NO_FAULT),

    # ── codex-cli 0.146.0 (0.147.0 bump: rig re-run, streams measured
    #    shape-identical — fixtures keep their recorded truth) ───────────────
    # Reaches empty_completion only because error items are bucketed apart from
    # tool activity: with `-m` naming a model the backend does not advertise,
    # codex emits an `item.completed` whose item type is "error" (`Model
    # metadata for stub-model not found`) BEFORE `turn.started`, and scoring
    # that as tool work made this arm unreachable against every local backend.
    ("codex_empty", EMPTY),
    # Counterfactual for the row above: same scenario, argv differs only in `-m`.
    ("codex_empty_no_model", EMPTY),
    ("codex_healthy", NO_FAULT),
    # `agent_message` counts only when its text is non-blank, like
    # `_claude_activity`: the `-o` file here is four whitespace bytes and
    # `codex_text_dump` is empty, so nothing was actually said.
    ("codex_whitespace", EMPTY),
    # Tool work with no closing message is not a fault, whatever the counters say.
    ("codex_tool_only", NO_FAULT),
    ("codex_refusal", NO_FAULT),
    ("codex_http_400", TERMINAL),
    # 12, not 15, and the status comes from the STREAM: codex's stderr is the
    # same 39 bytes of `Reading additional input from stdin...` on every failure
    # and matches no auth marker at all. Exit 15 would be correlation-eligible,
    # so an expired key rolled out to a whole Dev Type would read as a shared
    # backend outage and be excused instead of latching the auth breaker.
    ("codex_http_401", AUTH),
    # Same 401 with codex's DEFAULT retry policy — five `Reconnecting... N/5`
    # `error` events before the bare one — proving the retry noise changes
    # nothing either way.
    ("codex_http_401_retrying", AUTH),
    ("codex_http_429", TERMINAL),
    ("codex_http_500", TERMINAL),
    # 404 on /v1/responses — a backend that lacks the route at all. Same family
    # as the HTTP rows above: a hard error the harness reports and cannot retry.
    ("codex_no_route", TERMINAL),
    # `no_terminal_event` is UNREACHABLE for codex, so `terminal_error` is the
    # intended reason here and not a concession: every codex failure path emits a
    # plain {"type":"error"} immediately before `turn.failed`
    # (test_codex_always_emits_an_error_event_before_turn_failed), so a stream
    # with no `turn.completed` always has an error message to classify on.
    ("codex_truncated", TERMINAL),
    ("codex_truncated_retrying", TERMINAL),

    # ── grok-build 0.2.112 ───────────────────────────────────────────────────
    ("grok_healthy", NO_FAULT),
    ("grok_refusal", NO_FAULT),
    # 16 real tool executions, and the stream is ONE line long (the `end` event).
    # Conservative only because the `grok export` transcript lists the tool calls.
    ("grok_tool_only", NO_FAULT),
    # 200-with-nothing: grok emits type:error (empty response), not empty_completion.
    ("grok_empty", TERMINAL),
    # empty_completion for grok: export echoes the prompt; only prompt-anchored
    # parse sees that nothing followed (`grok_export_activity`).
    ("grok_whitespace", EMPTY),
    # grok's stderr matches only the GENERIC `unauthorized` marker, which ranks
    # below the predicate; the 12 comes from the in-band `Unauthorized (401)`.
    ("grok_http_401", AUTH),
    ("grok_http_429", TERMINAL),
    ("grok_http_500", TERMINAL),
    # The truncation is reported as `reqwest error stream: Transport error` on
    # the same `error` event, so `no_terminal_event` is unreachable for grok too.
    ("grok_truncated", TERMINAL),
    # Announced twice in band — a `{"type":"max_turns_reached"}` event and `end`
    # with `stopReason:"Cancelled"` — but only the event TYPE decides, and it is
    # checked FIRST so a deterministic cap can never become correlation-eligible.
    ("grok_turn_budget", BUDGET),
    # grok silent non-progress halt (repeated identical tool call) — NO_FAULT;
    # run still lands exit 11 for missing result.json (docs/15 §2b).
    ("grok_loop_nocap", NO_FAULT),
    ("grok_loop_cap30", NO_FAULT),
    # Control: varying tool args pass 16; --max-turns 20 stops loudly (exit 16).
    ("grok_loop_varying_cap20", BUDGET),
    # Operator misconfig (duplicate --output-format) → exit 10, not correlation.
    ("grok_json_blob", CRASH),

    # ── headless resume (ADR-0022 Phase 0) ───────────────────────────────────
    # Each pair is one rig run: a first leg through harness_argv, then a second
    # leg in the SAME workspace through harness_resume_argv, resuming the first
    # leg's session. Every leg exits 0 with no fault (the stub writes no
    # result.json, so all six still land exit 11 — the row-9 shape ADR-0022's
    # loop takes over). What each pair PROVES, and what RESUME_SPECS encodes:
    #   grok 0.2.117:  `-p P -r SID` composes headless; the resumed `end` event
    #     carries the SAME sessionId (no fork), no history replay in stdout,
    #     usage/num_turns are PER-INVOCATION (leg 2 reports 1 turn, one call's
    #     tokens) → usage_cumulative=False. Version drift note: 0.2.117 says
    #     stopReason "end_turn" where the 0.2.112 captures say "EndTurn", and
    #     adds available_commands/usage event types — nothing branches on
    #     either (GROK_FAULT_STOP_REASONS is empty by design; unknown event
    #     types are skipped), the docs/08 unpinned-CLI caveat in action.
    ("grok_resume_nudge", NO_FAULT),
    ("grok_resume_nudge_resume", NO_FAULT),
    #   claude 2.1.210: `-p P --resume SID` composes headless; same session_id
    #     on the resumed result event, no replay, per-invocation usage
    #     → usage_cumulative=False.
    ("claude_resume_nudge", NO_FAULT),
    ("claude_resume_nudge_resume", NO_FAULT),
    #   codex 0.146.0: `exec resume THREAD P` composes headless; same
    #     thread_id, no replay, and usage is CUMULATIVE over the session —
    #     leg 2's turn.completed reports input 240/output 48, both calls'
    #     worth (docs/08 §5's `codex exec resume` caveat, now measured)
    #     → usage_cumulative=True. Summing a resume chain would double-count.
    ("codex_resume_nudge", NO_FAULT),
    ("codex_resume_nudge_resume", NO_FAULT),
]


@pytest.mark.parametrize("name,intended",
                         [pytest.param(n, i, id=n) for n, i in CAPTURES])
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
# Facts about the captured bytes, not about the predicate — they were true before
# the fix commit and are true after it. They are what the table's comments assert
# in prose, and what a predicate change has to keep agreeing with.

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


def test_the_model_pin_adds_an_error_item_before_the_turn_starts():
    """`Model metadata … not found` arrives as an `item.completed` before
    `turn.started`, and its item type is "error" — neither `agent_message` nor
    a tool item. `codex_run_fault` used to score it as tool activity, which is
    what made `empty_completion` unreachable against every local backend; it now
    has its own bucket and is evidence only."""
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
    """The evidence for the 12: the status is in the stream, and the only
    channel the pre-ADR-0018 classifier read says nothing about it — so the
    in-band extractor is the whole difference between 12 and 15 here."""
    stream = (FIXTURES / "codex_http_401.jsonl").read_text()
    stderr = companion("codex_http_401", "stderr.txt")
    assert "401 Unauthorized" in stream
    assert "401" not in stderr and stderr.strip() == "Reading additional input from stdin..."
    assert ep.classify_harness_failure(stderr) == 10
    assert ep.auth_evidence_is_distinctive(stderr) is False
    assert ep.harness_api_error_status("codex", stream) == 401
    # …and PRECISION, the reason the pattern is anchored on codex's own
    # transport wording: codex echoes the server's response BODY verbatim into
    # `error.message`, so a generic `status (\\d{3})` would let a backend put
    # "status 401" inside a 500 and pause the whole Dev Type.
    body_echo = (FIXTURES / "codex_http_400.jsonl").read_text()
    assert "invalid_request_error" in body_echo
    assert ep.harness_api_error_status("codex", body_echo) is None


def test_grok_export_echoes_the_prompt_so_dump_is_never_empty():
    """The finding that inverted grok's `empty_completion`: the whole transcript
    of a run that produced nothing useful is the prompt read back — and the
    prompt-anchored parse is what sees through it."""
    dump = companion("grok_whitespace", "dump.txt")
    assert dump.startswith("## User") and "ACKNOWLEDGED" in dump
    text, session = ep.grok_stream_parse((FIXTURES / "grok_whitespace.jsonl").read_text())
    assert text.strip() == "" and session
    assert dump.strip()          # the naive `dump.strip()` test still says "worked"
    assert ep.grok_export_activity(dump, capture_prompt(META["grok_whitespace"])) is False
    for name in ("grok_healthy", "grok_refusal", "grok_tool_only", "grok_turn_budget"):
        assert ep.grok_export_activity(companion(name, "dump.txt"),
                                       capture_prompt(META[name])) is True
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
    for name in ("grok_turn_budget", "grok_loop_varying_cap20"):
        assert [e.get("type") for e in events(name)] == ["max_turns_reached", "end"]
        assert events(name)[1]["stopReason"] == "Cancelled"
        assert companion(name, "stderr.txt").strip() == "Error: max turns reached"
        assert META[name]["exit_code"] == 1


def test_grok_halts_silently_on_a_repeated_tool_call():
    """THE operational hazard, and why `--max-turns` is not the lever for it.

    Answered with the byte-identical tool call every turn, grok 0.2.112 ends the
    run ITSELF at 16 model calls — and the stream it leaves is
    indistinguishable from a clean success: one `end` event, `EndTurn`, exit 0,
    no `max_turns_reached`. Nothing was completed, so no `result.json` exists and
    the run is reported exit 11 `DEV_BAD_OUTPUT` with no hint of the cause. The
    predicate is CORRECT not to fault it (16 tool executions really happened,
    and the export lists them) — the hazard is the silence, not the verdict.
    """
    for name in ("grok_loop_nocap", "grok_loop_cap30", "grok_tool_only"):
        evs = events(name)
        assert [e.get("type") for e in evs] == ["end"]           # nothing else
        assert evs[0]["stopReason"] == "EndTurn"                 # says "success"
        assert evs[0]["num_turns"] == 16
        assert evs[0]["modelUsage"]["stub-model"]["modelCalls"] == 16
        assert META[name]["exit_code"] == 0 and not companion(name, "stderr.txt")
        # …and DevCake's honest report is the bad-output code, before and after
        # ADR-0018 — this row is untouched by the fault predicate
        assert (META[name]["devcake_exit_now"],
                META[name]["devcake_exit_before_adr0018"]) == (11, 11)


def test_grok_stops_at_16_only_when_the_tool_call_repeats():
    """The control that rules out a 16-turn ceiling — the alternative reading of
    the three rows above, and the one an operator would act on wrongly.

    `grok_loop_cap30` ASKED for 30 turns and got 16, which looks exactly like a
    cap that cannot be raised. It is not: the `loop_varying` lane differs only in
    that the tool arguments move (`stub_backend.py::vary`), and the same CLI then
    sails past 16 and is stopped by `--max-turns 20` at `num_turns: 20`. The 30
    was accepted and armed — the halt just fired first and the run never reached
    turn 30. `--max-turns` is fully actionable on grok, above 16 like anywhere
    else; what stops the repeated-call runs is grok's own non-progress halt,
    which it announces in the tool RESULT ("You appear to be running empty
    commands…") and nowhere in its output stream.
    """
    varying = events("grok_loop_varying_cap20")[1]
    assert varying["num_turns"] == 20                        # > 16, and loud
    assert varying["modelUsage"]["stub-model"]["modelCalls"] == 20
    assert "--max-turns" in META["grok_loop_varying_cap20"]["argv"]
    # the cap30 row asked for MORE than the varying row got, and got less
    assert META["grok_loop_cap30"]["argv"][-2:] == ["--max-turns", "30"]
    assert events("grok_loop_cap30")[0]["num_turns"] == 16
    # 20 distinct calls in the export vs 16 identical ones — the whole difference
    tools = [ln for ln in companion("grok_loop_varying_cap20", "dump.txt").splitlines()
             if ln.startswith("- ")]
    assert len(tools) == 20 and len(set(tools)) == 20
    repeated = [ln for ln in companion("grok_loop_nocap", "dump.txt").splitlines()
                if ln.startswith("- ")]
    assert len(repeated) == 16 and len(set(repeated)) == 1


def test_grok_401_reaches_exit_12_on_the_in_band_status_not_on_stderr():
    """grok's stderr wording is auth wording, but only the GENERIC kind, and the
    generic arm ranks BELOW the predicate — which now finds a fault on every one
    of these rows. So the 12 this capture reached before ADR-0018 is preserved
    only because the status is extracted from the stream."""
    stderr = companion("grok_http_401", "stderr.txt")
    stream = (FIXTURES / "grok_http_401.jsonl").read_text()
    assert "Unauthorized (401)" in stderr
    assert ep.classify_harness_failure(stderr) == 12
    assert ep.auth_evidence_is_distinctive(stderr) is False
    assert ep.harness_api_error_status("grok-build", stream) == 401
    assert META["grok_http_401"]["devcake_exit_before_adr0018"] == 12
    # and the neighbouring statuses stay out of the auth lane
    assert ep.harness_api_error_status(
        "grok-build", (FIXTURES / "grok_http_500.jsonl").read_text()) == 500
    assert ep.harness_api_error_status(
        "grok-build", (FIXTURES / "grok_http_429.jsonl").read_text()) is None


def test_grok_json_blob_never_ran_the_model():
    """The row where the CLI refused its own argv: zero bytes of stdout, exit 2,
    and a clap usage error naming the duplicated flag."""
    assert (FIXTURES / "grok_json_blob.jsonl").read_text() == ""
    assert META["grok_json_blob"]["exit_code"] == 2
    assert "--output-format" in companion("grok_json_blob", "stderr.txt")
    assert "cannot be used multiple times" in companion("grok_json_blob", "stderr.txt")
