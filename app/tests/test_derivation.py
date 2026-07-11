"""INV-1 (PMO is the single source of truth: type is a pure function of live
PMO state) and INV-2 (at most one stage label) — docs/02 §2, every row."""
from datetime import datetime, timezone

from devcake.pmo import Mission, MissionType, derive

NOW = datetime.now(timezone.utc)


def m(status="backlog", labels=(), kind="issue"):
    return Mission(pmo_id="x", pmo_kind=kind, key="T-1", title="t",
                   status=status, labels=set(labels), updated_at=NOW)


def test_row1_onboard():
    d = derive(m(labels={"DEVCAKE"}), "opt_in")
    assert d.mission_type == MissionType.ONBOARD and d.schedulable


def test_rows234_stage_labels():
    for label, t in [("DEVCAKE-PLAN", MissionType.PLAN),
                     ("DEVCAKE-EXECUTE", MissionType.EXECUTE),
                     ("DEVCAKE-REVIEW", MissionType.REVIEW)]:
        d = derive(m("in_progress", {"DEVCAKE", label}), "opt_in")
        assert d.mission_type == t and d.schedulable


def test_row5_terminal_ignored():
    assert not derive(m("done", {"DEVCAKE", "DEVCAKE-PLAN"}), "opt_in").schedulable


def test_row6_conflict_inv2():
    d = derive(m(labels={"DEVCAKE", "DEVCAKE-PLAN", "DEVCAKE-EXECUTE"}), "opt_in")
    assert not d.schedulable and "CONFLICT" in d.reason


def test_rows78_skip_failed_override():
    for guard in ("DEVCAKE-SKIP", "DEVCAKE-FAILED"):
        d = derive(m(labels={"DEVCAKE", "DEVCAKE-PLAN", guard}), "opt_in")
        assert not d.schedulable


def test_row9_human_work_untouched():
    d = derive(m("in_progress", {"DEVCAKE"}), "opt_in")
    assert d.mission_type is None and not d.schedulable


def test_row10_merge_sweep_owned():
    d = derive(m("in_progress", {"DEVCAKE", "DEVCAKE-MERGE"}), "opt_in")
    assert not d.schedulable and "merge" in d.reason


def test_adoption_gate_opt_in_vs_out():
    assert not derive(m(), "opt_in").schedulable          # unlabeled → ignored
    assert derive(m(), "opt_out").schedulable             # opt-out adopts all


def test_purity_no_local_state_inv1():
    a, b = derive(m(labels={"DEVCAKE"}), "opt_in"), derive(m(labels={"DEVCAKE"}), "opt_in")
    assert a == b                                          # same input, same answer
