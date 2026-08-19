"""MCP / entrypoint-setup gate (docs/07 §5 step 5).

Public seams:
  run_mcp_setup(commands, workdir, timeout=300) — discrete additive lines
  (stdin closed, new session, per-command cap, stop at first failure).
  The harness-phase entrypoint must *call* that chokepoint (not only
  re-export it) before launching the dialect.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

# host checkout: repo/app/tests → repo/images/common; app container: /srv/tests
# → /srv/images/common (read-only compose mount)
_COMMON_CANDIDATES = [
    Path(__file__).parents[2] / "images" / "common",
    Path(__file__).parents[1] / "images" / "common",
    Path("/srv/images/common"),
]
COMMON = next((p for p in _COMMON_CANDIDATES if (p / "devcake_dev").is_dir()),
              _COMMON_CANDIDATES[0])
ENTRYPOINT = COMMON / "dev_entrypoint.py"
if str(COMMON) not in sys.path:
    sys.path.insert(0, str(COMMON))

from devcake_dev.workspace.setup import (  # noqa: E402
    MCP_SETUP_TIMEOUT_SECS,
    run_mcp_setup,
)

# Entrypoint load is only for credential-merge (lives on the façade) and the
# wiring honesty check. Restore env afterwards — leaking REDIS_URL would
# repoint live messaging tests at a nonexistent server.
_ENV_KEYS = ("DEVCAKE_RUN_ID", "REDIS_URL", "REDIS_USER", "REDIS_PASSWORD")
_saved = {k: os.environ.get(k) for k in _ENV_KEYS}
os.environ.setdefault("DEVCAKE_RUN_ID", "T-1-1-EXECUTE-AAAAAA")
os.environ.setdefault("REDIS_URL", "redis://localhost:6399/0")
os.environ.setdefault("REDIS_USER", "test")
os.environ.setdefault("REDIS_PASSWORD", "test")

spec = importlib.util.spec_from_file_location("dev_entrypoint_mcp", ENTRYPOINT)
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)

for _k, _v in _saved.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


def test_all_commands_pass(tmp_path):
    assert run_mcp_setup(["true", "echo ok"], tmp_path) is None
    assert run_mcp_setup([], tmp_path) is None


def test_failure_carries_detail_and_stops_at_first(tmp_path):
    """The first failing command aborts the sequence (later commands may
    depend on it) and returns the command + exit code + stderr tail — the
    material for the exit-14 artifact."""
    cmd = "sh -c 'echo boom >&2; exit 3'"
    failed = run_mcp_setup([cmd, f"touch {tmp_path}/marker"], tmp_path)
    assert failed is not None
    fcmd, detail = failed
    assert fcmd == cmd
    assert "exit 3" in detail and "boom" in detail
    assert not (tmp_path / "marker").exists()   # stopped at first failure


def test_hung_command_times_out_promptly(tmp_path):
    """A hung command (registry stall, interactive prompt) dies at the
    per-command cap, not the run's wall clock — the whole process group is
    killed so grandchildren can't keep the pipes open."""
    t0 = time.monotonic()
    failed = run_mcp_setup(["sleep 30"], tmp_path, timeout=1)
    assert time.monotonic() - t0 < 10
    assert failed == ("sleep 30", "timed out after 1s")


def test_stdin_is_closed(tmp_path):
    """A command that reads stdin sees immediate EOF instead of waiting on
    an interactive answer forever."""
    assert run_mcp_setup(["head -c1"], tmp_path, timeout=15) is None


def test_default_timeout_is_300s():
    """Documented contract: 300 s per additive command (docs/07 §5)."""
    assert MCP_SETUP_TIMEOUT_SECS == 300


def test_entrypoint_calls_run_mcp_setup():
    """Production harness phase must invoke the chokepoint — a bare import /
    façade re-export is the false-green this mission retires."""
    src = ENTRYPOINT.read_text()
    assert "façade re-export" not in src
    # A real call site, not merely the import name.
    assert re.search(r"\brun_mcp_setup\s*\(", src), (
        "dev_entrypoint must call run_mcp_setup(...) for additive setup")


def test_credential_json_merges_over_baked_file(tmp_path, monkeypatch):
    """Uploaded settings must not clobber baked privacy/auto-update keys."""
    dest = tmp_path / "settings.json"
    dest.write_text(
        '{"privacy":{"usageStatisticsEnabled":false},'
        '"general":{"enableAutoUpdate":false}}')
    monkeypatch.setenv("HOME", str(tmp_path))
    ep._write_credential_files({
        "credential_files": [{
            "path_hint": str(dest),
            "content": '{"selectedType":"openai","privacy":{"foo":1}}',
        }],
    })
    data = json.loads(dest.read_text())
    assert data["privacy"]["usageStatisticsEnabled"] is False
    assert data["privacy"]["foo"] == 1
    assert data["general"]["enableAutoUpdate"] is False
    assert data["selectedType"] == "openai"
