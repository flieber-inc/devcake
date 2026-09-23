"""Cgroup v2 is the Dev container's RAM and CPU budget.

`nproc` and `/proc/meminfo` show the host. The entrypoint reads
`cpu.max` and `memory.max` and exports tool limits from those files.
Unlimited (`max`) exports nothing for that resource.
"""
import importlib.util
import os
from pathlib import Path

_CANDIDATES = [Path(__file__).parents[2] / "images" / "common" / "dev_entrypoint.py",
               Path(__file__).parents[1] / "images" / "common" / "dev_entrypoint.py"]
ENTRYPOINT = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

_ENV_KEYS = ("DEVCAKE_RUN_ID", "REDIS_URL", "REDIS_USER", "REDIS_PASSWORD")
_saved = {k: os.environ.get(k) for k in _ENV_KEYS}
os.environ.setdefault("DEVCAKE_RUN_ID", "T-1-1-EXECUTE-AAAAAA")
os.environ.setdefault("REDIS_URL", "redis://localhost:6399/0")
os.environ.setdefault("REDIS_USER", "test")
os.environ.setdefault("REDIS_PASSWORD", "test")
spec = importlib.util.spec_from_file_location("dev_entrypoint_cgroup_budget", ENTRYPOINT)
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)
for _k, _v in _saved.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


def test_quota_and_memory_max_become_tool_limits():
    # 200000/100000 = 2 CPUs. 4294967296 bytes = 4 GiB.
    # 70% of that cap is the number the tools are told, computed here
    # as integer arithmetic, not by calling the function under test.
    got = ep.cgroup_budget("200000 100000\n", "4294967296\n")
    assert got["DEVCAKE_CPUS"] == "2"
    assert got["DEVCAKE_MEMORY_BYTES"] == "4294967296"
    assert got["GOMAXPROCS"] == "2"
    assert got["GOMEMLIMIT"] == "3006477107B"
    assert got["NODE_OPTIONS"] == "--max-old-space-size=2867"


def test_unlimited_cgroup_exports_nothing():
    assert ep.cgroup_budget("max 100000", "max") == {}
    assert ep.cgroup_budget("200000 100000", "max")["DEVCAKE_CPUS"] == "2"
    assert "GOMEMLIMIT" not in ep.cgroup_budget("200000 100000", "max")
    assert "GOMAXPROCS" not in ep.cgroup_budget("max 100000", "4294967296")


def test_apply_reads_the_files_and_does_not_clobber(tmp_path):
    cpu = tmp_path / "cpu.max"
    mem = tmp_path / "memory.max"
    cpu.write_text("200000 100000\n")
    mem.write_text("4294967296\n")
    env = {"NODE_OPTIONS": "--require /opt/hook.js"}
    ep.apply_cgroup_budget(env, cpu, mem)
    assert env["DEVCAKE_CPUS"] == "2"
    assert env["GOMAXPROCS"] == "2"
    assert env["NODE_OPTIONS"] == "--require /opt/hook.js"
    missing = tmp_path / "nope"
    untouched = {}
    ep.apply_cgroup_budget(untouched, missing, missing)
    assert untouched == {}
