"""In-container continuation (ADR-0022) — the pure decision helpers.

Resume mechanics run against the REAL capture pairs in fixtures/harness_streams/
(`*_resume_nudge` / `*_resume_nudge_resume`): each pair is one rig run whose
second leg went through the production `harness_resume_argv` in the same
workspace, so these tests assert what the CLIs actually did, not what their
help text promises.
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
spec = importlib.util.spec_from_file_location("dev_entrypoint_continuation",
                                              ENTRYPOINT)
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)

for _k, _v in _saved.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

FIXTURES = Path(__file__).parent / "fixtures" / "harness_streams"


def fx(name):
    return (FIXTURES / name).read_text()


def meta(name):
    return json.loads(fx(f"{name}.meta.json"))


def J(obj):
    return json.dumps(obj)


# ── session_identity — the neutral per-harness session handle ────────────────

@pytest.mark.parametrize("harness,capture", [
    ("grok-build", "grok_resume_nudge"),
    ("claude-code", "claude_resume_nudge"),
    ("codex", "codex_resume_nudge"),
])
def test_session_identity_matches_the_rig_measurement(harness, capture):
    """The handle the loop would resume with is the one the rig measured and
    actually resumed with (meta.session_id fed the second leg's argv)."""
    assert ep.session_identity(harness, fx(f"{capture}.jsonl")) \
        == meta(capture)["session_id"]


@pytest.mark.parametrize("harness,capture", [
    ("grok-build", "grok_resume_nudge"),
    ("claude-code", "claude_resume_nudge"),
    ("codex", "codex_resume_nudge"),
])
def test_resume_leg_kept_the_first_legs_session(harness, capture):
    """No fork on any harness: the resumed stream reports the SAME handle the
    first leg exposed — the fact export_sids' last-per-chain rule rests on."""
    resumed = meta(f"{capture}_resume")
    assert resumed["first_session_id"] == meta(capture)["session_id"]
    assert ep.session_identity(harness, fx(f"{capture}_resume.jsonl")) \
        == resumed["first_session_id"]


def test_session_identity_absent_is_empty_string():
    assert ep.session_identity("grok-build", J({"type": "error", "message": "x"})) == ""
    assert ep.session_identity("codex", J({"type": "turn.started"})) == ""
    assert ep.session_identity("claude-code", J({"type": "assistant"})) == ""
    assert ep.session_identity("grok-build", "") == ""


def test_session_identity_never_raises_on_hostile_shapes():
    hostile = "\n".join([
        J({"type": "end", "sessionId": {"nested": True}}),
        J({"type": "thread.started", "thread_id": ["a"]}),
        "not json", J([1, 2])])
    for harness in ("grok-build", "codex", "claude-code", "unknown"):
        assert isinstance(ep.session_identity(harness, hostile), str)


# ── harness_resume_argv — the per-harness resume dialects ────────────────────

def test_grok_resume_argv_matches_the_captured_composition():
    """The builder reproduces the argv the capture proved (modulo the ids),
    keeping the same output format and approval mode as harness_argv so the
    stream parsers and fault predicate see the identical shape."""
    argv = ep.harness_resume_argv("grok-build", "SID", "NUDGE",
                                  model="stub-model")
    captured = meta("grok_resume_nudge_resume")["argv"]
    assert argv == ["grok", "-p", "NUDGE", "-r", "SID",
                    "--output-format", "streaming-json", "--always-approve",
                    "--model", "stub-model"]
    assert captured[:2] == ["grok", "-p"]
    assert captured[3] == "-r" and "--always-approve" in captured


def test_claude_resume_argv_matches_the_captured_composition():
    argv = ep.harness_resume_argv("claude-code", "SID", "NUDGE")
    assert argv == ["claude", "-p", "NUDGE", "--resume", "SID",
                    "--output-format", "stream-json", "--verbose",
                    "--dangerously-skip-permissions"]
    assert meta("claude_resume_nudge_resume")["argv"][3] == "--resume"


def test_codex_resume_argv_matches_the_captured_composition():
    argv = ep.harness_resume_argv("codex", "SID", "NUDGE", out_dir="/o")
    assert argv[:5] == ["codex", "exec", "resume", "SID", "NUDGE"]
    assert "--json" in argv and "/o/last_message.txt" in argv
    assert meta("codex_resume_nudge_resume")["argv"][1:3] == ["exec", "resume"]


def test_resume_argv_extras_come_last_so_they_can_override():
    for harness in ("grok-build", "claude-code", "codex"):
        argv = ep.harness_resume_argv(harness, "S", "P", extra=["--zz", "1"])
        assert argv[-2:] == ["--zz", "1"]


def test_resume_argv_unknown_harness_is_none_not_the_claude_fallback():
    """Both builders fail closed on unknown ids — resume already returned
    None; harness_argv now raises instead of falling through to Claude."""
    assert ep.harness_resume_argv("other", "S", "P") is None


# ── RESUME_SPECS — capture-verified facts only ───────────────────────────────

def test_resume_specs_cover_exactly_the_captured_harnesses():
    assert set(ep.RESUME_SPECS) == {"grok-build", "claude-code", "codex"}


def test_codex_usage_is_cumulative_and_the_others_are_not():
    """The measured per-harness fact behind ResumeSpec.usage_cumulative: the
    codex resume leg's turn.completed reports BOTH calls' tokens (240/48 vs
    the first leg's 120/24); grok and claude report one invocation's worth.
    Token VALUES are the stub's; the aggregation behaviour is the CLI's own
    (fixtures README). RE-MEASURED at 0.147.0 (audit B4: openai/codex#35621
    'skip restored token usage replay' did NOT change the final
    turn.completed aggregation — still cumulative on our wire path); the >0
    guard kills the (0, 0) degeneracy the 2× relation would tolerate."""
    def usage(name, kind, key):
        for line in fx(f"{name}.jsonl").splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if isinstance(ev, dict) and ev.get("type") == kind:
                return (ev.get("usage") or {}).get(key)
        return None
    first_in = usage("codex_resume_nudge", "turn.completed", "input_tokens")
    first_out = usage("codex_resume_nudge", "turn.completed", "output_tokens")
    assert first_in and first_in > 0 and first_out and first_out > 0
    assert usage("codex_resume_nudge_resume", "turn.completed", "input_tokens") \
        == 2 * first_in
    assert usage("codex_resume_nudge_resume", "turn.completed", "output_tokens") \
        == 2 * first_out
    assert usage("grok_resume_nudge_resume", "end", "input_tokens") \
        == usage("grok_resume_nudge", "end", "input_tokens")
    assert ep.RESUME_SPECS["codex"].usage_cumulative is True
    assert ep.RESUME_SPECS["grok-build"].usage_cumulative is False
    assert ep.RESUME_SPECS["claude-code"].usage_cumulative is False


# ── continuation_config — env parsing that must never exit 20 ────────────────

def cfg(budget="", policy=""):
    return ep.continuation_config({"DEVCAKE_MAX_CONTINUATIONS": budget,
                                   "DEVCAKE_CONTINUATION_POLICY": policy})


def test_continuation_config_defaults_and_garbage_never_raise():
    assert cfg().max_continuations == 0 and cfg().policy == "auto"
    assert ep.continuation_config({}).max_continuations == 0
    assert cfg("3", "auto").max_continuations == 3
    assert cfg("50").max_continuations == 50          # founder: large budgets legal
    assert cfg("-2").max_continuations == 0            # negative → off
    assert cfg("banana").max_continuations == 0        # garbage → off, not exit 20
    assert cfg("2", "resume-only").policy == "resume-only"
    assert cfg("2", "fresh-only").policy == "fresh-only"
    assert cfg("2", "off").policy == "off"
    assert cfg("2", "RESUME").policy == "auto"         # unknown → auto; budget gates


# ── next_continuation — the decision matrix ──────────────────────────────────

def decide(policy="auto", budget=2, state=None, *, plan_mode=False, sid="S",
           supported=True, fp="f1"):
    state = state if state is not None else ep.ContinuationState()
    d = ep.next_continuation(ep.ContinuationConfig(budget, policy), state,
                             plan_mode=plan_mode, session_id=sid,
                             resume_supported=supported, fingerprint=fp)
    return d, state


def test_plan_mode_never_continues():
    d, s = decide(plan_mode=True)
    assert d.action == "stop" and s.used == 0


def test_off_and_zero_budget_stop():
    assert decide(policy="off")[0].action == "stop"
    assert decide(budget=0)[0].action == "stop"


def test_budget_is_the_only_terminator():
    """Escalated + budget remaining → fresh, NOT stop (founder decision:
    zero progress escalates the mode, never ends the run)."""
    s = ep.ContinuationState(used=1, escalated_to_fresh=True,
                             last_fingerprint="f1")
    d, s = decide(budget=50, state=s, fp="f1")     # stalled AND escalated
    assert d.action == "fresh" and s.used == 2
    d, _ = decide(budget=2, state=ep.ContinuationState(used=2))
    assert d.action == "stop" and "budget exhausted" in d.reason


def test_auto_prefers_resume_then_escalates_permanently_on_stall():
    d, s = decide()                                 # first landing: resume
    assert d.action == "resume" and s.used == 1 and s.last_fingerprint == "f1"
    d, s = decide(budget=5, state=s, fp="f1")       # same fingerprint → stall
    assert d.action == "fresh" and s.escalated_to_fresh
    d, s = decide(budget=5, state=s, fp="f2")       # progress resumes…
    assert d.action == "fresh"                      # …but escalation is a LATCH


def test_unknown_fingerprint_never_latches():
    d, s = decide()
    assert d.action == "resume"
    d, s = decide(budget=5, state=s, fp=None)       # git broke: progress unknown
    assert d.action == "resume" and not s.escalated_to_fresh


def test_auto_degrades_to_fresh_without_resume():
    assert decide(sid="")[0].action == "fresh"          # no handle captured
    assert decide(supported=False)[0].action == "fresh"  # no verified resume


def test_resume_only_stops_rather_than_degrade():
    """The operator explicitly forbade fresh sessions — fail as today."""
    d, s = decide(policy="resume-only", sid="")
    assert d.action == "stop" and s.used == 0
    assert decide(policy="resume-only", supported=False)[0].action == "stop"
    assert decide(policy="resume-only")[0].action == "resume"


def test_fresh_only_never_resumes():
    d, _ = decide(policy="fresh-only")
    assert d.action == "fresh"


# ── nudge prompts ─────────────────────────────────────────────────────────────

LEGAL = {"reviewed", "human_needed"}


def test_resume_nudge_names_path_outcomes_and_the_imperative():
    p = ep.resume_nudge_prompt("REVIEW", LEGAL, attempt=1, budget=3)
    assert "/workspace/out/result.json" in p
    assert '"human_needed" | "reviewed"' in p          # sorted legal outcomes
    assert "do NOT end your turn" in p
    assert "continuation 1/3" in p
    # NEVER a restated JSON shape: the per-type contracts carry required
    # fields (verdict/report_md, pr_url) a reduced example would drop
    assert "Required output" in p and "schema_version" not in p
    assert "do NOT open" in p                          # PR-duplication guard


def test_resume_nudge_carries_the_stray_note_iff_present():
    assert "Note: " not in ep.resume_nudge_prompt("REVIEW", LEGAL,
                                                  attempt=1, budget=2)
    p = ep.resume_nudge_prompt("REVIEW", LEGAL, attempt=1, budget=2,
                               stray_note="found result.json at /workspace/repo")
    assert "found result.json at /workspace/repo" in p


def test_fresh_nudge_embeds_the_original_prompt_verbatim():
    original = "IDENTIFYING\n\n## PLAYBOOK with {literal} braces\nrule 7"
    p = ep.fresh_nudge_prompt(original, "EXECUTE", {"executed"},
                              attempt=2, budget=5)
    assert original in p                               # the frozen assembly rides whole
    assert "IN THIS WORKING TREE" in p
    assert "git status" in p and "/workspace/activity/" in p
    assert "do NOT redo finished work" in p
    assert p.index("ORIGINAL MISSION INSTRUCTIONS") < p.index(original[:20])


# ── session chains ────────────────────────────────────────────────────────────

def test_record_session_chains_and_export():
    chains = []
    ep.record_session(chains, "a", "initial")
    ep.record_session(chains, "a", "resume")           # same sid: no-op
    assert chains == [["a"]]
    ep.record_session(chains, "b", "resume")           # fork: extends the chain
    assert chains == [["a", "b"]]
    ep.record_session(chains, "c", "fresh")            # new chain
    assert chains == [["a", "b"], ["c"]]
    ep.record_session(chains, "", "fresh")             # empty sid: no-op
    assert chains == [["a", "b"], ["c"]]
    assert ep.export_sids(chains) == ["b", "c"]        # last per chain
    assert ep.last_sid(chains) == "c"
    assert ep.last_sid([]) == ""


# ── merge_token_reports ───────────────────────────────────────────────────────

# loading ep above put images/common on sys.path (the entrypoint's own boot)
from devcake_dev.harness.tokens import token_report_v1 as _v1  # noqa: E402

R1 = _v1(input_tokens=100, output_tokens=10, total_tokens=110,
         model="m", source="end_event", num_turns=4)
R2 = _v1(input_tokens=50, output_tokens=5, total_tokens=55,
         model="m", source="end_event", num_turns=2)


def test_single_report_is_returned_unchanged():
    """The zero-continuation payload stays byte-identical — including its
    scalar extras (reasoning_tokens)."""
    solo = {**R1, "reasoning_tokens": 7}
    assert ep.merge_token_reports([solo], ["initial"],
                                  resume_cumulative=False) == solo


def test_non_cumulative_chains_sum_fieldwise():
    merged = ep.merge_token_reports([R1, R2], ["initial", "resume"],
                                    resume_cumulative=False)
    assert merged["input_tokens"] == 150 and merged["num_turns"] == 6
    assert merged["cost_usd_native"] is None           # all-None stays None, not 0
    assert merged["model"] == "m" and merged["source"] == "end_event"


def test_cumulative_resume_chain_is_last_wins_but_chains_still_sum():
    """codex: a resumed terminal event already contains the whole chain —
    summing would double-count; a later FRESH chain still adds. The chain
    report names the cumulative provenance (ADR-0029)."""
    cumulative = {**R2, "input_tokens": 150, "output_tokens": 15,
                  "total_tokens": 165, "num_turns": 6}
    merged = ep.merge_token_reports([R1, cumulative], ["initial", "resume"],
                                    resume_cumulative=True)
    assert merged["input_tokens"] == 150               # last-wins, not 250
    assert merged["source"] == "cumulative"
    fresh = {**R1, "input_tokens": 30, "output_tokens": 3, "total_tokens": 33,
             "num_turns": 1}
    merged = ep.merge_token_reports([R1, cumulative, fresh],
                                    ["initial", "resume", "fresh"],
                                    resume_cumulative=True)
    assert merged["input_tokens"] == 180               # 150 (chain) + 30 (fresh)


def test_merge_is_none_safe_and_handles_the_unavailable_stub():
    stub = ep.unavailable_report()
    merged = ep.merge_token_reports([R1, stub], ["initial", "fresh"],
                                    resume_cumulative=False)
    assert merged["input_tokens"] == 100               # sum of the values present
    assert merged["source"] == "mixed"
    assert ep.merge_token_reports([], [], resume_cumulative=False)[
        "source"] == "unavailable"


# ── merged_transcript_dump ────────────────────────────────────────────────────

def test_single_segment_passes_through_unlabeled():
    assert ep.merged_transcript_dump(["body"], ["initial"]) == "body"
    assert ep.merged_transcript_dump(["", "body"],
                                     ["initial", "continuation 1"]) == "body"
    assert ep.merged_transcript_dump([], []) == ""


def test_multiple_segments_are_labeled_and_joined():
    dump = ep.merged_transcript_dump(["one", "two"],
                                     ["initial", "continuation 1 (fresh)"])
    assert "## Continuation segment — initial" in dump
    assert "## Continuation segment — continuation 1 (fresh)" in dump
    assert dump.index("one") < dump.index("two")


# ── workspace_fingerprint (injectable runner, like _git_tracked) ─────────────

class _R:
    def __init__(self, code=0, out="x"):
        self.returncode, self.stdout = code, out


def test_fingerprint_deterministic_and_sensitive():
    def runner(argv, **kw):
        return _R(out="HEAD\n" if "rev-parse" in argv else "M file\n")
    a = ep.workspace_fingerprint("/w", runner=runner)
    assert a == ep.workspace_fingerprint("/w", runner=runner)
    def runner2(argv, **kw):
        return _R(out="HEAD\n" if "rev-parse" in argv else "M file\nM other\n")
    assert a != ep.workspace_fingerprint("/w", runner=runner2)
    def runner3(argv, **kw):
        return _R(out="OTHER\n" if "rev-parse" in argv else "M file\n")
    assert a != ep.workspace_fingerprint("/w", runner=runner3)


def test_fingerprint_is_none_when_git_fails():
    assert ep.workspace_fingerprint("/w", runner=lambda a, **k: _R(code=128)) is None
    def boom(argv, **kw):
        raise OSError("no git")
    assert ep.workspace_fingerprint("/w", runner=boom) is None


def test_resumed_streams_do_not_replay_history():
    """The parser-safety fact: a resumed stream carries ONLY the new turn's
    events (one text/assistant burst, one terminal event) — no re-emission of
    the first leg's messages that would pollute result_text or the token
    report."""
    grok_kinds = [json.loads(l).get("type")
                  for l in fx("grok_resume_nudge_resume.jsonl").splitlines()]
    assert grok_kinds.count("end") == 1
    claude_kinds = [json.loads(l).get("type")
                    for l in fx("claude_resume_nudge_resume.jsonl").splitlines()]
    assert claude_kinds.count("assistant") == 1 and claude_kinds.count("result") == 1
    codex_kinds = [json.loads(l).get("type")
                   for l in fx("codex_resume_nudge_resume.jsonl").splitlines()]
    assert codex_kinds.count("turn.completed") == 1
