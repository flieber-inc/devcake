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
        "deny /sys/firmware/** rwklx,\n"
        "deny @{PROC}/kcore rwklx,\n")
    assert m._covered("/proc/kcore", rules, "r")
    assert m._covered("/proc/acpi", rules, "r") and m._covered("/proc/scsi/x", rules, "r")
    assert m._covered("/proc/bus", rules, "w") and m._covered("/proc/irq/9", rules, "w")
    assert m._covered("/sys/firmware", rules, "r")
    # `/proc/*` is ONE level: it covers /proc/foo, never /proc/foo/bar
    assert m._covered("/proc/foo", rules, "w")
    assert m._covered("/proc/sys", rules, "w")
    assert not m._covered("/proc/foo/bar", rules, "w")
    assert not m._covered("/proc/newthing/sub", rules, "w")
    # a character-class parent never counts as a literal parent
    assert m._covered("/sys/firmware/x", rules, "w")            # the literal rule
    assert not m._covered("/sys/kernel/debug", rules, "w")      # only /sys/[^f]*/**
    # the permission asked for must be in the rule
    assert not m._covered("/proc/bus", rules, "r")
    assert not m._covered("/proc/foo", rules, "r")


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
    readonly = ["/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"]
    assert all(m._covered(p, rules, "r") for p in masked)
    assert all(m._covered(p, rules, "w") for p in readonly)
