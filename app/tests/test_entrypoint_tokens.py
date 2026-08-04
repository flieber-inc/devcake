"""Grok token extraction (INV-5) — driven by the real 0.2.112 captures.

At 0.2.112 the terminal `end` event carries the whole report inline, so the
entrypoint reads stdout it already parses instead of hunting `signals.json` in
the session directory (docs/08 §5). Both paths are asserted here: the `end`
event is preferred, `signals.json` remains the fallback, and each names itself
truthfully in `extraction_method`.

**What is and is not evidence.** The token *values* below come from the capture
rig's stub backend (`scripts/harness_capture/stub_backend.py`) — they are pinned
here as literals so a mapping change (input↔output, cache read, totals) fails
loudly, but they say nothing about real grok billing. The *presence and key
names* of `usage` / `num_turns` / `modelUsage`, and the ABSENCE of any cost
field, are the CLI's own behaviour (fixtures README).
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
spec = importlib.util.spec_from_file_location("dev_entrypoint_tokens", ENTRYPOINT)
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)

for _k, _v in _saved.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

FIXTURES = Path(__file__).parent / "fixtures" / "harness_streams"


def stream(name: str) -> str:
    return (FIXTURES / f"{name}.jsonl").read_text()


def exit_code(name: str) -> int:
    return json.loads((FIXTURES / f"{name}.meta.json").read_text())["exit_code"]


# Every grok capture that reaches a terminal turn, with the numbers its `end`
# event actually carries. `exit` is the process status the rig measured: the
# last row is a FAILED run that still reports in full.
WITH_END_EVENT = {
    "grok_healthy":     {"exit": 0, "in": 120,  "out": 24,  "total": 144,  "turns": 1},
    "grok_refusal":     {"exit": 0, "in": 120,  "out": 24,  "total": 144,  "turns": 1},
    "grok_whitespace":  {"exit": 0, "in": 120,  "out": 24,  "total": 144,  "turns": 1},
    "grok_tool_only":   {"exit": 0, "in": 1920, "out": 384, "total": 2304, "turns": 16},
    "grok_turn_budget": {"exit": 1, "in": 240,  "out": 48,  "total": 288,  "turns": 2},
}

# The rest: an `error` event or nothing at all, so no usage anywhere in stdout
# (`grok_json_blob` never ran — clap rejected the duplicated --output-format).
WITHOUT_END_EVENT = ["grok_empty", "grok_http_401", "grok_http_429",
                     "grok_http_500", "grok_truncated", "grok_json_blob"]


@pytest.mark.parametrize("name", sorted(WITH_END_EVENT))
def test_end_event_fills_the_report_from_stdout(name):
    want = WITH_END_EVENT[name]
    assert exit_code(name) == want["exit"]           # the capture is what we think
    report = ep.grok_end_report(ep.grok_end_event(stream(name)))
    assert report["source"] == "end_event"
    assert report["input_tokens"] == want["in"]
    assert report["output_tokens"] == want["out"]
    assert report["cache_read_tokens"] == 0
    assert report["total_tokens"] == want["total"]
    assert report["num_turns"] == want["turns"]
    assert report["model"] == "stub-model"           # from modelUsage, not argv
    # ADR-0029: first-class scalar, no longer a `notes` string to regex
    assert report["reasoning_tokens"] == 0


def test_a_failed_run_reports_in_full():
    # grok_turn_budget exits 1 (--max-turns 2) and still carries a complete
    # `end` event — the case signals.json cannot serve, since it is written
    # only for cleanly-ended sessions (fixtures README).
    assert exit_code("grok_turn_budget") == 1
    report = ep.grok_end_report(ep.grok_end_event(stream("grok_turn_budget")))
    assert report["source"] == "end_event"
    assert report["output_tokens"] == 48 and report["num_turns"] == 2


def test_cost_is_none_because_grok_reports_none():
    # A 0 here would read as "this run was free" in the feed report and would
    # aggregate as real spend on devcake.cost.usd.
    report = ep.grok_end_report(ep.grok_end_event(stream("grok_healthy")))
    assert report["cost_usd_native"] is None
    assert report["cost_usd_native"] != 0


@pytest.mark.parametrize("name", sorted(WITH_END_EVENT) + WITHOUT_END_EVENT)
def test_no_grok_capture_carries_any_cost_field(name):
    def costs(node):
        if isinstance(node, dict):
            return any("cost" in str(k).lower() for k in node) \
                or any(costs(v) for v in node.values())
        return isinstance(node, list) and any(costs(v) for v in node)

    for line in stream(name).splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        assert not costs(ev), f"{name}: a cost field appeared — revisit cost_usd"


@pytest.mark.parametrize("name", WITHOUT_END_EVENT)
def test_streams_with_no_end_event_yield_no_report(name):
    # None, not a report of nulls: the caller must be free to fall back to
    # signals.json and then to "unavailable".
    assert ep.grok_end_event(stream(name)) is None
    assert ep.grok_end_report(ep.grok_end_event(stream(name))) is None


def test_the_last_end_event_wins():
    out = "\n".join([json.dumps({"type": "end", "usage": {"output_tokens": 1}}),
                     json.dumps({"type": "text", "data": "more"}),
                     json.dumps({"type": "end", "usage": {"output_tokens": 9}})])
    assert ep.grok_end_report(ep.grok_end_event(out))["output_tokens"] == 9


def test_dominant_model_is_the_one_that_produced_most():
    ev = {"type": "end", "usage": {"output_tokens": 30},
          "modelUsage": {"small": {"outputTokens": 10},
                         "big": {"outputTokens": 20, "modelCalls": 2}}}
    assert ep.grok_end_report(ev)["model"] == "big"


@pytest.mark.parametrize("ev", [
    None, "end", 7, [], {}, {"type": "end"},
    {"type": "end", "usage": None}, {"type": "end", "usage": "none"},
    {"type": "end", "usage": []},
])
def test_hostile_shapes_report_nothing_rather_than_raising(ev):
    assert ep.grok_end_report(ev) is None


@pytest.mark.parametrize("mu", ["stub", ["stub"], 3, {"stub": "1200"}, {"stub": None}])
def test_a_broken_model_usage_never_aborts_the_report(mu):
    # model-controlled nested data on the failure path: a truthy non-dict is
    # exactly what `x or {}` fails to rescue
    report = ep.grok_end_report({"type": "end", "usage": {"input_tokens": 5},
                                 "modelUsage": mu, "num_turns": "two"})
    assert report["input_tokens"] == 5
    assert report["model"] in ("grok", "stub")
    assert report["num_turns"] == "two"      # reported as found, never invented


# ── signals.json — the retained fallback (docs/08 §5) ────────────────────────

def _session(home: Path, sid: str, payload: dict) -> Path:
    d = home / ".grok" / "sessions" / "%2Fworkspace%2Frepo%2Fdemo" / sid
    d.mkdir(parents=True)
    (d / "signals.json").write_text(json.dumps(payload))
    return d


def test_signals_json_still_reports_when_there_is_no_end_event(tmp_path):
    _session(tmp_path, "sess-1", {"contextTokensUsed": 22006, "turnCount": 3,
                                  "modelsUsed": ["grok-code-fast-1"],
                                  "contextWindowTokens": 256000})
    report = ep.grok_signals_report("sess-1", home=tmp_path)
    # ADR-0029: names its ACTUAL path — pre-v1 this masqueraded as
    # "session_json", indistinguishable from the claude/codex extraction
    assert report["source"] == "signals"
    assert report["total_tokens"] == 22006
    assert report["num_turns"] == 3
    assert report["model"] == "grok-code-fast-1"
    assert report["cost_usd_native"] is None               # totals only, no cost


def test_signals_json_falls_back_to_total_tokens_key(tmp_path):
    _session(tmp_path, "sess-2", {"totalTokens": 26892})
    assert ep.grok_signals_report("sess-2", home=tmp_path)["total_tokens"] == 26892


@pytest.mark.parametrize("sid,payload", [
    ("", {"contextTokensUsed": 1}),      # an `error` event carries no sessionId
    ("other", {"contextTokensUsed": 1}),  # crashed run: no session on disk
])
def test_signals_json_reports_nothing_when_there_is_nothing_to_read(
        tmp_path, sid, payload):
    _session(tmp_path, "sess-3", payload)
    assert ep.grok_signals_report(sid, home=tmp_path) is None


@pytest.mark.parametrize("models", [None, [], "grok-4", {"grok-4": 1}, 5])
def test_signals_json_model_list_shapes_never_raise(tmp_path, models):
    _session(tmp_path, "sess-4", {"contextTokensUsed": 9, "modelsUsed": models})
    report = ep.grok_signals_report("sess-4", home=tmp_path)
    assert report["total_tokens"] == 9
    assert isinstance(report["model"], str)
