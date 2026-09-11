"""scripts/check_apparmor_masks.py — the profile's deny list must cover what
Docker masks; the coverage logic itself must not be fooled by a
single-level or character-class rule (the case a future Docker mask
addition would slip through)."""
import importlib.util
from pathlib import Path

import pytest

_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "scripts" / "check_apparmor_masks.py",
    Path("/srv/repo-scripts/check_apparmor_masks.py"),
]


def _mod():
    src = next((p for p in _CANDIDATES if p.is_file()), None)
    if src is None:
        pytest.skip("scripts/ not mounted")
    spec = importlib.util.spec_from_file_location("check_apparmor_masks", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_coverage_logic_is_not_fooled_by_single_level_or_class_rules():
    m = _mod()
    rules = m.deny_rules(
        "deny @{PROC}/* w,\n"
        "deny @{PROC}/{acpi,asound,scsi}/{,**} rwklx,\n"
        "deny @{PROC}/{bus,fs,irq}/** wklx,\n"
        "deny /sys/[^f]*/** wklx,\n"
        "deny /sys/firmware/{,**} rwklx,\n"
        "deny /sys/kernel/security/** rwklx,\n"
        "deny @{PROC}/kcore rwklx,\n")
    assert m._covered("/proc/kcore", rules, "r")
    assert m._covered("/proc/acpi", rules, "r") and m._covered("/proc/scsi/x", rules, "r")
    assert m._covered("/sys/firmware", rules, "r") and m._covered("/sys/firmware/dmi", rules, "r")
    # `X/**` denies what is BELOW X, never X itself
    assert m._covered("/sys/kernel/security/x", rules, "r")
    assert not m._covered("/sys/kernel/security", rules, "r")
    assert m._covered("/proc/bus", rules, "w")               # itself via /proc/* (one level)
    assert not m._covered("/sys/kernel/security", rules, "w")   # /** only, nothing for itself
    assert m._covered("/proc/irq/9", rules, "w")
    # `/proc/*` is ONE level: it covers /proc/foo, never /proc/foo/bar
    assert m._covered("/proc/foo", rules, "w")
    assert not m._covered("/proc/foo/bar", rules, "w")
    # a character-class parent never counts as a literal parent
    assert not m._covered("/sys/kernel/debug", rules, "w")
    # the permission asked for must be in the rule
    assert not m._covered("/proc/foo", rules, "r")
    # a read-only TREE needs the path and everything below it
    assert m._tree_covered("/sys/firmware", rules, "r")
    assert m._tree_covered("/proc/bus", rules, "w")          # itself via /proc/*, below via /**
    assert not m._tree_covered("/proc/foo", rules, "w")      # one level only, nothing below
    assert not m._tree_covered("/sys/kernel/security", rules, "r")   # below yes, itself no
    # a literal rule on a DIRECTORY is not a tree; on a file it is enough
    dir_only = m.deny_rules("deny @{PROC}/bus w,\ndeny @{PROC}/sysrq-trigger rwklx,\n")
    assert not m._tree_covered("/proc/bus", dir_only, "w", is_dir=True)
    assert m._tree_covered("/proc/sysrq-trigger", dir_only, "w", is_dir=False)


def test_a_failed_directory_probe_is_loud_not_a_weaker_check(monkeypatch):
    """The read-only tree rule depends on knowing which entries are
    directories; a probe container that did not run must abort, never
    silently fall back to the file-only check."""
    import subprocess
    m = _mod()
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["docker", "create"]:
            return subprocess.CompletedProcess(cmd, 0, "cid\n", "")
        if cmd[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, '["/proc/kcore"]\n["/proc/sys"]\n', "")
        if cmd[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(cmd, 125, "", "unknown option systempaths")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as exc:
        m.docker_lists()
    assert "directory probe did not run" in str(exc.value)


def test_a_profile_missing_a_deny_fails_the_check(monkeypatch, tmp_path):
    """The checker exists for the day Docker extends its lists: a profile
    lacking one deny must fail, not pass."""
    m = _mod()
    weak = tmp_path / "weak"
    weak.write_text("deny @{PROC}/* w,\ndeny /sys/firmware/** rwklx,\n")   # dir itself open
    monkeypatch.setattr(m, "docker_lists", lambda: (["/sys/firmware"], ["/proc/bus"], {"/proc/bus"}))
    assert m.main(["x", str(weak)]) == 1
    good = tmp_path / "good"
    good.write_text("deny /sys/firmware/{,**} rwklx,\ndeny @{PROC}/bus/{,**} wklx,\n")
    assert m.main(["x", str(good)]) == 0
    busonly = tmp_path / "busonly"
    busonly.write_text("deny /sys/firmware/{,**} rwklx,\ndeny @{PROC}/bus w,\n")   # dir, not tree
    assert m.main(["x", str(busonly)]) == 1


def test_the_shipped_profile_covers_dockers_current_lists():
    m = _mod()
    prof = next((p for p in (
        Path(__file__).resolve().parents[2] / "scripts" / "apparmor" / "devcake-nested",
        Path("/srv/repo-scripts/apparmor/devcake-nested")) if p.is_file()), None)
    if prof is None:
        pytest.skip("profile not mounted")
    rules = m.deny_rules(prof.read_text())
    masked = ["/proc/acpi", "/proc/asound", "/proc/interrupts", "/proc/kcore",
              "/proc/keys", "/proc/latency_stats", "/proc/sched_debug", "/proc/scsi",
              "/proc/timer_list", "/proc/timer_stats", "/sys/devices/virtual/powercap",
              "/sys/firmware"]
    readonly = ["/proc/bus", "/proc/fs", "/proc/irq", "/proc/sysrq-trigger"]
    assert all(m._covered(p, rules, "r") for p in masked)
    assert all(m._tree_covered(p, rules, "w", is_dir=(p != "/proc/sysrq-trigger"))
               for p in readonly)
    assert m._covered("/proc/sys", rules, "w") and "/proc/sys" in m.PARTIAL_READONLY
