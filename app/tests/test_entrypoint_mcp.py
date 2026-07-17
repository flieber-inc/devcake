"""run_mcp_setup units (docs/07 §5 step 5): the entrypoint's MCP setup
runner must stop at the first failure with a detail worth putting in the
exit-14 artifact, cap each command's runtime (the heartbeat daemon is
already beating — a hung install would otherwise idle to the wall clock),
and never block on stdin."""

import importlib.util
import os
import time
from pathlib import Path

# host checkout: repo/app/tests → repo/images/common; app container: /srv/tests
# → /srv/images/common (read-only compose mount)
_CANDIDATES = [Path(__file__).parents[2] / "images" / "common" / "dev_entrypoint.py",
               Path(__file__).parents[1] / "images" / "common" / "dev_entrypoint.py"]
ENTRYPOINT = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

# module reads these at import; redis.from_url is lazy (no connection until
# use). Restore the env afterwards — leaking REDIS_URL here would repoint the
# live messaging tests (same pytest process) at a nonexistent server.
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
    assert ep.run_mcp_setup(["true", "echo ok"], tmp_path) is None
    assert ep.run_mcp_setup([], tmp_path) is None


def test_failure_carries_detail_and_stops_at_first(tmp_path):
    """The first failing command aborts the sequence (later commands may
    depend on it) and returns the command + exit code + stderr tail — the
    material for the exit-14 artifact."""
    cmd = "sh -c 'echo boom >&2; exit 3'"
    failed = ep.run_mcp_setup([cmd, f"touch {tmp_path}/marker"], tmp_path)
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
    failed = ep.run_mcp_setup(["sleep 30"], tmp_path, timeout=1)
    assert time.monotonic() - t0 < 10
    assert failed == ("sleep 30", "timed out after 1s")


def test_stdin_is_closed(tmp_path):
    """A command that reads stdin sees immediate EOF instead of waiting on
    an interactive answer forever."""
    assert ep.run_mcp_setup(["head -c1"], tmp_path, timeout=15) is None
