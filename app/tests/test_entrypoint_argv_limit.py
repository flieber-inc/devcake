"""docs/07 §4 exit 17 — the prompt rides the harness command line as one
argument; past the kernel's per-argument ceiling the launch would die at
execve before any heartbeat. The entrypoint refuses first, with the
numbers, as its own class (field: the discovery steward's package crossed
131,072 bytes and every run died as "dagu run dead" for four days)."""
import importlib.util
import os
from pathlib import Path

import pytest

_CANDIDATES = [Path(__file__).parents[2] / "images" / "common" / "dev_entrypoint.py",
               Path(__file__).parents[1] / "images" / "common" / "dev_entrypoint.py"]
ENTRYPOINT = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

_ENV_KEYS = ("DEVCAKE_RUN_ID", "REDIS_URL", "REDIS_USER", "REDIS_PASSWORD")
_saved = {k: os.environ.get(k) for k in _ENV_KEYS}
os.environ.setdefault("DEVCAKE_RUN_ID", "T-1-1-EXECUTE-AAAAAA")
os.environ.setdefault("REDIS_URL", "redis://localhost:6399/0")
os.environ.setdefault("REDIS_USER", "test")
os.environ.setdefault("REDIS_PASSWORD", "test")
spec = importlib.util.spec_from_file_location("dev_entrypoint_argv_limit", ENTRYPOINT)
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)
for _k, _v in _saved.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


def test_ordinary_argv_is_not_too_large():
    assert ep.argv_too_large(["grok", "-p", "fix the bug", "--output-format",
                              "streaming-json"]) is None
    assert ep.argv_too_large([]) is None


def test_one_argument_past_the_kernel_ceiling_is_refused_with_the_numbers():
    cap = ep._arg_strlen_max()
    assert cap >= 131072                         # 32 pages, 4 KiB or larger
    fine = ep.argv_too_large(["grok", "-p", "x" * cap])
    assert fine is None                          # at the cap still launches
    detail = ep.argv_too_large(["grok", "-p", "x" * (cap + 1)])
    assert detail is not None
    assert str(cap + 1) in detail and str(cap) in detail
    assert "cannot launch" in detail


def test_total_command_line_past_arg_max_is_refused():
    cap = ep._arg_strlen_max()
    many = ["grok"] + ["y" * (cap - 1)] * 20     # each under the per-arg cap
    detail = ep.argv_too_large(many)
    assert detail is not None and "in total" in detail


def test_refusal_exits_17_with_a_classified_artifact(monkeypatch):
    sent = []
    monkeypatch.setattr(ep, "send_artifacts", lambda payload: sent.append(payload))
    with pytest.raises(SystemExit) as exc:
        ep._fail_prompt_too_large(None, "one command-line argument is 1145938 bytes")
    assert exc.value.code == 17
    assert len(sent) == 1
    p = sent[0]
    assert p["exit_code"] == 17 and p["error_class"] == "DEV_PROMPT_TOO_LARGE"
    assert "1145938" in p["error_detail"] and p["result"] is None
    assert "token_report" in p and "transcript_md" in p
