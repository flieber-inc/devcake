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

This module opened as the EVIDENCE half of a two-commit sequence: eleven rows
landed as strict expected failures naming the mechanism that produced the wrong
answer, and the fix commit was finished exactly when every one of them had been
removed. All forty-seven rows now hold against the working-tree predicate, so
the table is a plain regression suite: any row that stops holding is a real
change of verdict, and every verdict change is a behaviour change an operator
feels (15 excuses an attempt and feeds the correlation brake, 12 pauses a whole
Dev Type, 16 is always counted).

Seventeen rows are `live_*`: captures against the REAL model backend that
produced the 2026-07-24 production incident, rather than against a stub. They
reproduce that incident exactly — codex exits 0, makes no tool call, writes no
`result.json`, and DevCake reports exit 11 `DEV_BAD_OUTPUT` both before and
after ADR-0018 — and every one of them intends NO FAULT, because the model did
answer; it answered with prose. They are the false-positive guard for anything
that would fault a run on the content of a model-controlled string. They are
also the only fixtures here that cannot be regenerated, so their sidecars carry
`reproducible: false` and a `scrub` record of the backend-address placeholder.

Three rows carry a fact about a HARNESS rather than about the predicate: grok
halts a repeated-tool-call run itself at 16 model calls, silently and with exit
0 (`grok_loop_nocap`, `grok_loop_cap30`). That is a non-progress halt, NOT a turn
cap — `grok_loop_varying_cap20` runs past 16 on the same lane and is stopped by
`--max-turns 20` at turn 20. Their verdicts are unremarkable; the tests below
them are the point.
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


def capture_prompt(meta: dict) -> str:
    """The prompt the rig actually sent, read back out of the recorded argv.

    It is argv[2] for all three harnesses (`claude -p P`, `grok -p P`,
    `codex exec P`) because the rig builds argv through the entrypoint's own
    `harness_argv`. grok's `empty_completion` arm needs it: the `grok export`
    transcript opens by echoing the prompt, and the prompt is what separates
    that echo from anything the run itself produced.
    """
    return meta["argv"][2]


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

    # ── codex-cli 0.144.4 ────────────────────────────────────────────────────
    # Reaches empty_completion only because error items are bucketed apart from
    # tool activity: with `-m` naming a model the backend does not advertise,
    # codex emits an `item.completed` whose item type is "error" (`Model
    # metadata for stub-model not found`) BEFORE `turn.started`, and scoring
    # that as tool work made this arm unreachable against every local backend.
    ("codex_empty", EMPTY),
    # THE controlled counterfactual for the row above: same stub scenario, same
    # CLI, argv differing only in the `-m stub-model` pair.
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
    # EXPECTATION CHANGED by the fix commit (from empty_completion, same exit).
    # grok reports 200-with-nothing as an `{"type":"error"}` event naming its own
    # condition — `empty response from model (no_visible_content)` — and the new
    # arm fires on that EVENT TYPE, carrying grok's verbatim message into the
    # detail. Demoting it back to empty_completion would take a match on the
    # message STRING, which buys nothing here: the exit code is 15 either way,
    # and terminal_error is the more informative of the two.
    ("grok_empty", TERMINAL),
    # THE grok incident row, and the only one `empty_completion` still fires on:
    # `grok export` opens by echoing the prompt, so `dump.strip()` is truthy for
    # every run that has a session id. Only the prompt-anchored parse
    # (`grok_export_activity`) sees that the transcript is the echo and nothing
    # else.
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
    # ── grok's SILENT NON-PROGRESS HALT, and the control that explains it ────
    # Both are the `loop` lane — the byte-identical tool call, every turn — and
    # both stop at 16 model calls with `EndTurn` and exit 0, one of them having
    # asked for `--max-turns 30` (accepted and armed; the halt fired at 16, so
    # turn 30 was never reached — this is NOT a cap the flag failed to raise).
    # NO FAULT is the correct verdict and not a concession: 16 tool executions
    # happened, the export lists them, and the predicate is right to see
    # activity. It is what happens NEXT that hurts — no `result.json` was
    # written, so the honest report is exit 11 `DEV_BAD_OUTPUT`,
    # indistinguishable from a Dev that simply misbehaved
    # (`test_grok_halts_silently_on_a_repeated_tool_call`, `15` §2b).
    ("grok_loop_nocap", NO_FAULT),
    ("grok_loop_cap30", NO_FAULT),
    # THE control, and the row that keeps the remedy honest: the same lane with
    # the tool ARGUMENTS varied runs straight past 16, and `--max-turns 20` then
    # stops it loudly at `num_turns: 20`. So the halt is not a ceiling and the
    # cap is genuinely raisable on grok — `15-errors-and-retries.md` §2a's
    # advice is correct as written.
    ("grok_loop_varying_cap20", BUDGET),
    # NOT a harness fault: grok 0.2.112 exits 2 at argument parsing when
    # EXTRA_ARGS adds a second --output-format, so the CLI never ran and stdout
    # is zero bytes. Calling that a harness fault would make an operator
    # misconfiguration correlation-eligible and deterministic-across-the-fleet —
    # exactly what turn_budget is separated out to avoid — while stderr carries
    # the precise clap diagnosis that exit 10 surfaces.
    ("grok_json_blob", CRASH),

    # ── LIVE captures against the REAL incident backend (2026-07-25) ─────────
    # Not reproducible: a real vLLM serving DeepSeek-V4-Flash-DSpark-Abliterated,
    # sampled once. Provenance is each sidecar's `model` + `captured_at`, and
    # every sidecar records `reproducible: false` and the address `scrub`.
    #
    # EVERY ONE OF THEM IS `NO_FAULT`, AND THAT IS THE FINDING — not a
    # concession. The 2026-07-24 production incident reproduces here exactly
    # (codex exits 0, writes no result.json, DevCake reports exit 11
    # DEV_BAD_OUTPUT), and ADR-0018 does NOT reclassify it to 15, because the
    # model DID speak: a long, non-blank `agent_message` containing invented
    # tool syntax as prose. `empty_completion` is structural and there is
    # nothing empty here; the only thing that would separate this from a
    # legitimately chatty run is the CONTENT of a model-controlled string, and
    # firing an arm on that is precisely what the predicate refuses to do
    # (HARNESS_STATUS_PATTERNS' comment states the rule). So these rows are a
    # false-positive guard: if a future predicate change starts faulting them,
    # it has begun grading prose.
    #
    # codex on the :8765 proxy — THE incident path.
    ("live_codex_proxy_trivial", NO_FAULT),      # control: the path is healthy
    ("live_codex_proxy_mission", NO_FAULT),      # THE reproduction
    # codex on raw :8000 — the A/B. The proxy exists to reposition the system
    # prompt for codex, so this is the known-degenerate config; it degenerates
    # the SAME way, which is what removes "the proxy hiccuped" from the list of
    # explanations.
    ("live_codex_raw_trivial", NO_FAULT),
    ("live_codex_raw_mission", NO_FAULT),
    # The controls that rule out the backend and the model: same server, same
    # model id, same prompt, different harness — and both of these actually
    # finished the task on disk (result.json written, `farewell` added).
    ("live_claude_raw_mission", NO_FAULT),
    ("live_grok_raw_mission", NO_FAULT),
    # An operator could point claude or grok at the codex-shaped :8765 proxy by
    # mistake. Recorded so the predicate is graded on that misconfiguration too
    # — and it turns out not to be a failure at all for either of them.
    ("live_claude_proxy_trivial", NO_FAULT),
    ("live_grok_proxy_trivial", NO_FAULT),
    # LANE 2 — eight concurrent runs at the deployment's real `global_max: 8`,
    # against the incident path. All eight land identically, which is the
    # CASCADE character of the incident ("every other container failed
    # identically") rather than merely its verdict.
    *[(f"live_codex_proxy_conc8.{i}", NO_FAULT) for i in range(8)],
    # The escalation to N=16 produced one slot that behaved differently under
    # saturation: 9,666 output tokens of degenerate repetition over 232 s,
    # ~40x its siblings. Same verdict. Committed because it is the only
    # measured instance of load changing the GENERATION, and it shows that even
    # a completely degenerate one does not become classifiable.
    ("live_codex_proxy_conc16.8", NO_FAULT),
]

# The live rows, by the argument each group carries. Used by the tests below so
# a capture cannot be added to one group and silently graded by another.
LIVE_CODEX_SINGLE = ("live_codex_proxy_trivial", "live_codex_proxy_mission",
                     "live_codex_raw_trivial", "live_codex_raw_mission")
LIVE_CONC8 = tuple(f"live_codex_proxy_conc8.{i}" for i in range(8))
LIVE_CODEX = LIVE_CODEX_SINGLE + LIVE_CONC8 + ("live_codex_proxy_conc16.8",)
LIVE_ALL = tuple(n for n, _ in CAPTURES if n.startswith("live_"))


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


# ── the LIVE batch: the 2026-07-24 incident, reproduced against the real
#    backend that caused it ────────────────────────────────────────────────
# Everything below is a fact about bytes sampled once from a real model. They
# cannot be regenerated, which is why the sidecars carry `reproducible: false`
# — but once committed they are as deterministic as any other fixture, so the
# verdicts above are re-derived from them normally rather than read back out of
# a sidecar.


def codex_item_counts(name: str) -> dict:
    """`{item type: count}` over a codex stream's `item.completed` events —
    the same three buckets `codex_run_fault` sorts them into."""
    counts = {}
    for ev in events(name):
        if ev.get("type") == "item.completed":
            item = ev["item"]
            key = item.get("item_type") or item.get("type")
            counts[key] = counts.get(key, 0) + 1
    return counts


def test_the_live_incident_reproduces_exactly_as_reported():
    """THE headline row.

    The production report was: a container failed `exit 11 — result.json
    missing`, and under pre-ADR-0018 code exit 11 required the CLI to exit **0**
    — so whatever happened, codex thought it had succeeded. That is what this
    capture is, taken against the same backend through the same `:8765` proxy
    with the founder's own `-c` block:

      * codex exits 0 and completes a turn, so nothing failed as far as it knows;
      * it made ZERO tool calls — no `command_execution`, no `file_change`;
      * what it produced instead is prose containing INVENTED tool syntax, and
        its closing message asserts it wrote `out/result.json`;
      * nothing was written. (Verified on a host-mounted workspace at capture
        time: no `result.json`, `src/greet.py` unmodified. That is a fact about
        the run, not about these bytes, so it is recorded in the README.)

    DevCake therefore reports exit 11 `DEV_BAD_OUTPUT` — and reports the SAME
    thing before and after ADR-0018. The ADR did not launder this fault; it
    never claimed to. `empty_completion` is structural and this completion is
    not empty.
    """
    name = "live_codex_proxy_mission"
    assert codex_item_counts(name) == {"error": 1, "agent_message": 1}
    completed = [e for e in events(name) if e.get("type") == "turn.completed"]
    assert completed and completed[0]["usage"]["output_tokens"] > 0
    assert META[name]["exit_code"] == 0
    # the model claims the deliverable it never produced
    last = companion(name, "last_message.txt")
    assert "result.json" in last and "<exec>" in last
    # and DevCake's verdict is the incident's verdict, unchanged by the ADR
    assert verdict(name) == NO_FAULT
    assert (META[name]["devcake_exit_now"],
            META[name]["devcake_exit_before_adr0018"]) == (11, 11)


@pytest.mark.parametrize("name", LIVE_CODEX)
def test_no_live_codex_run_ever_made_a_tool_call(name):
    """Thirteen codex runs — two prompts, two ports, N=1/8/16 — and not one
    `command_execution` between them. Every stream is exactly the benign `-m`
    metadata error item plus a single `agent_message`, so the failure is
    uniform: codex never executes anything against this model, and the
    deliverable a Dev produces by writing files therefore never exists."""
    assert codex_item_counts(name) == {"error": 1, "agent_message": 1}
    assert META[name]["exit_code"] == 0
    assert verdict(name) == NO_FAULT
    assert (META[name]["devcake_exit_now"],
            META[name]["devcake_exit_before_adr0018"]) == (11, 11)
    # stderr opens with the same unrelated line the stub batch measured, and
    # names neither the model nor the missing deliverable — ADR-0018's central
    # premise, now confirmed against a real backend on the only channel
    # pre-ADR-0018 DevCake could read.
    stderr = companion(name, "stderr.txt")
    assert stderr.startswith("Reading additional input from stdin...")
    assert META[name]["model"] not in stderr and "result.json" not in stderr
    # codex's ONE genuinely diagnostic stderr line is absent here, and correctly
    # so: `no last agent message` fires when the `-o` file would be empty, and
    # these runs all produced a message. It was just worth nothing.
    assert "no last agent message" not in stderr
    assert META[name]["last_message_bytes"] > 0


def test_the_only_extra_stderr_line_in_the_batch_is_a_concurrency_artifact():
    """Twelve of the thirteen codex captures write exactly 39 bytes of stderr.
    The thirteenth writes one more line, and it is worth naming so it is not
    read as a backend signal: running eight codex processes inside ONE container
    makes them race on the shared `$HOME` skills directory. That is an artifact
    of how the rig produces concurrency (N processes, one container — the
    sidecars say so), not something a real eight-container fleet would hit, and
    it still carries no information about the run's outcome.
    """
    sizes = {n: META[n]["stderr_bytes"] for n in LIVE_CODEX}
    noisy = [n for n, size in sizes.items() if size != 39]
    assert noisy == ["live_codex_proxy_conc8.5"]
    extra = companion(noisy[0], "stderr.txt")
    assert "failed to install system skills" in extra
    assert "Directory not empty" in extra
    assert META[noisy[0]]["concurrency"] == 8
    # it changed nothing: the run still exited 0 with the same shape
    assert META[noisy[0]]["exit_code"] == 0
    assert verdict(noisy[0]) == NO_FAULT


def test_the_proxy_is_not_the_variable():
    """The A/B that removes "the proxy hiccuped" from the shortlist.

    `:8765` repositions the system prompt where codex expects it and `:8000` is
    raw vLLM, which for codex is a KNOWN-DEGENERATE config. Both ports answer a
    no-tool prompt perfectly and both degenerate identically on a mission-shaped
    one, so the difference between them explains nothing about the incident.
    """
    ports = {n: META[n]["preflight"]["url"] for n in LIVE_CODEX_SINGLE}
    assert "vllm-backend:8765" in ports["live_codex_proxy_mission"]
    assert "vllm-backend:8000" in ports["live_codex_raw_mission"]
    # the trivial prompt succeeds on BOTH — one word, exactly as asked
    for name in ("live_codex_proxy_trivial", "live_codex_raw_trivial"):
        assert companion(name, "last_message.txt").strip() == "ACKNOWLEDGED"
    # the mission-shaped prompt degenerates on BOTH, into the same shape
    for name in ("live_codex_proxy_mission", "live_codex_raw_mission"):
        assert "<exec>" in companion(name, "last_message.txt")
        assert codex_item_counts(name) == {"error": 1, "agent_message": 1}


def test_the_same_backend_and_model_serve_claude_and_grok_correctly():
    """The control that rules out the backend, the model and the network.

    Same server, same model id, same mission-shaped prompt — and the other two
    harnesses tool-call for real and finish the job. Whatever broke codex is
    not "the backend was down", which is the hypothesis a fleet-wide
    simultaneous failure invites.
    """
    model = "DeepSeek-V4-Flash-DSpark-Abliterated"
    assert {META[n]["model"] for n in LIVE_ALL} == {model}

    tools = [b["name"] for ev in events("live_claude_raw_mission")
             if ev.get("type") == "assistant"
             for b in ev.get("message", {}).get("content", [])
             if b.get("type") == "tool_use"]
    assert tools == ["Read", "Edit", "Bash", "Write"]      # it read, edited, wrote
    result = [e for e in events("live_claude_raw_mission") if e.get("type") == "result"]
    assert result and result[0]["is_error"] is False and result[0]["subtype"] == "success"

    # grok's tool work is visible only in its export, exactly as the stub batch
    # established — and it lists real file operations, not prose
    grok_dump = companion("live_grok_raw_mission", "dump.txt")
    assert "## Tools" in grok_dump
    for call in ("- Read: src/greet.py", "- Edit: src/greet.py",
                 "- Edit: out/result.json"):
        assert call in grok_dump
    assert events("live_grok_raw_mission")[-1]["stopReason"] == "EndTurn"


def test_eight_concurrent_runs_failed_identically_and_at_once():
    """LANE 2, and the CASCADE half of the incident report: "after which every
    other container failed identically".

    Eight mission-shaped codex runs at the deployment's real `global_max: 8`,
    concurrent against the incident path. All eight exit 0, none makes a tool
    call, all eight report exit 11 — so a whole board burns simultaneously with
    no classifiable fault anywhere in it. Concurrency multiplied the failure; it
    did not change its shape.
    """
    assert len(LIVE_CONC8) == 8
    for name in LIVE_CONC8:
        assert META[name]["concurrency"] == 8
        assert META[name]["timed_out"] is False
        assert codex_item_counts(name) == {"error": 1, "agent_message": 1}
        assert verdict(name) == NO_FAULT
    # a single container's worth of concurrency, and every slot is its own run
    assert len({META[n]["argv"][2] for n in LIVE_CONC8}) == 1      # same prompt
    assert len({events(n)[0]["thread_id"] for n in LIVE_CONC8}) == 8


def test_saturation_changes_the_generation_but_not_the_verdict():
    """The N=16 escalation's one anomaly, and the reason it is committed.

    Under 2x the deployment's concurrency one slot degenerated much further
    than its siblings — 9,666 output tokens over 232 s of repetitive
    non-progress, ~40x the stdout of the other fifteen — instead of the ~300
    tokens of invented tool syntax the unloaded runs produce. It is the only
    measured instance of backend pressure changing what the model emits.

    It still exits 0, still produces one `agent_message` and no tool call, and
    still reports exit 11. So pressure moves the generation and leaves the
    classification exactly where it was: there is no concurrency level in reach
    at which this fault becomes visible to the predicate.
    """
    name = "live_codex_proxy_conc16.8"
    meta = META[name]
    assert meta["concurrency"] == 16 and meta["timed_out"] is False
    completed = [e for e in events(name) if e.get("type") == "turn.completed"]
    assert completed[0]["usage"]["output_tokens"] == 9666
    assert meta["duration_ms"] > 200_000
    # ~40x its N=8 siblings, and still ONE message with no tool call
    assert meta["stdout_bytes"] > 20 * max(META[n]["stdout_bytes"] for n in LIVE_CONC8) / 8
    assert codex_item_counts(name) == {"error": 1, "agent_message": 1}
    assert verdict(name) == NO_FAULT


@pytest.mark.parametrize("name", LIVE_ALL)
def test_live_captures_declare_their_scrub_and_their_non_reproducibility(name):
    """Provenance guard for the only fixtures here that cannot be regenerated.

    Two claims have to survive review. First, the backend address was
    placeholdered — so the sidecar records the rewrite rather than leaving it
    silent, and the substitution keeps scheme and port, because the
    `:8765`-proxy / `:8000`-raw distinction is what the A/B above is made of.
    Second, a live capture is a sample, not a fixture that can be re-taken: the
    model id and `captured_at` are the whole of its provenance.
    """
    meta = META[name]
    assert meta["reproducible"] is False and meta["reproducibility_note"]
    assert meta["model"] and meta["captured_at"] and meta["cli_version"]
    scrub = meta["scrub"]
    assert scrub["placeholder"] == "vllm-backend"
    assert scrub["occurrences_rewritten"] > 0
    assert scrub["length_preserving"] is True
    # the rewrite touched sidecars only, so no committed stream byte moved
    assert scrub["found_in_stream_files"] is False
    for suffix in ("jsonl", "stderr.txt", "dump.txt", "last_message.txt"):
        assert "vllm-backend" not in companion(name, suffix)
    # …and the port survived it, on both sides of the A/B
    url = meta["preflight"]["url"]
    assert url.startswith("http://vllm-backend:")
    assert url.split(":")[2].split("/")[0] in ("8000", "8765")
