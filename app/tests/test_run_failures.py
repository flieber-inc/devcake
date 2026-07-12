"""Run-failure shipping to OpenObserve (docs/12 §6): the Dagu step error —
including the Dev container's stderr tail — must survive into the OO record,
redacted. Payload shapes below match a live Dagu v2.10.5 response."""

from devcake.dagu import extract_node_errors
from devcake.runs import failure_record
from devcake.state import Run

LIVE_STATUS = {
    "dagRunDetails": {
        "dagRunId": "PRJ-x-1-ONBOARD-29T37F",
        "statusLabel": "failed",
        "nodes": [{
            "step": {"name": "run_dev"},
            "statusLabel": "failed",
            "error": ("exit status 13\nrecent stderr (tail):\n"
                      "clone failed: fatal: Authentication failed for "
                      "'https://github_pat_" + "a" * 73 + "@github.com/x/y.git/'"),
        }],
    }
}


def _run(**kw) -> Run:
    base = dict(run_id="PRJ-x-1-ONBOARD-29T37F", mission_key="PRJ-x",
                mission_type="ONBOARD", dev_type="senior-dev", seq=1)
    return Run(**{**base, **kw})


def test_extract_node_errors_live_shape():
    errs = extract_node_errors(LIVE_STATUS)
    assert len(errs) == 1
    assert errs[0]["step"] == "run_dev"
    assert errs[0]["status"] == "failed"
    assert "clone failed" in errs[0]["error"]


def test_extract_node_errors_empty_cases():
    assert extract_node_errors(None) == []
    assert extract_node_errors({}) == []
    assert extract_node_errors({"dagRunDetails": {"nodes": [
        {"step": {"name": "run_dev"}, "statusLabel": "finished", "error": None}
    ]}}) == []


def test_failure_record_carries_redacted_stderr_tail():
    rec = failure_record(_run(traceparent="00-abc123-def456-01"), "failed",
                         "dagu run dead (never started)",
                         extract_node_errors(LIVE_STATUS))
    assert rec["run_id"] == "PRJ-x-1-ONBOARD-29T37F"
    assert rec["outcome"] == "failed"
    assert rec["trace_id"] == "abc123"
    assert "clone failed" in rec["detail"]          # the diagnosis survives
    assert "github_pat_" not in rec["detail"]       # the token does not


def test_failure_record_without_step_errors():
    rec = failure_record(_run(), "timed_out", "exceeded 7200s", [])
    assert rec["trace_id"] == ""
    assert rec["detail"] == "(no step error recorded in dagu)"
