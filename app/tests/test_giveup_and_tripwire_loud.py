"""CAKE-75 — give-up marks instance-scoped; give-up feed carries docs/15 §3
detail; out-of-pipeline-merge tripwire surfaces forge probe failures.

Public seams: markers.last_giveup_at, dispatch._give_up / _attempt_gate,
review._flag_out_of_pipeline_merge.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from devcake.config import AppConfig, DevType
from devcake.domain.model import Mission, MissionRef, MissionType
from devcake.domain.orchestrator import dispatch, markers, review
from devcake.domain.run import Run
from devcake.ports.forge import ForgeError, PullRequest
from fakes import NullMessaging, make_mission_manager


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


class _FeedFakePMO:
    """Minimal PMOPort stand-in that records feed posts and label swaps."""

    def __init__(self, mission: Mission):
        self.mission = mission
        self.comments: list[str] = []
        self.swaps: list[tuple[set, set]] = []

    def capabilities(self):
        from fakes import fake_pmo_capabilities
        return fake_pmo_capabilities()

    async def get(self, ref):
        assert isinstance(ref, MissionRef)
        return self.mission

    async def post_feed(self, ref, markdown):
        assert isinstance(ref, MissionRef)
        self.comments.append(markdown)

    async def swap_labels(self, ref, remove, add):
        assert isinstance(ref, MissionRef)
        self.swaps.append((set(remove), set(add)))
        self.mission.labels = (self.mission.labels - set(remove)) | set(add)

    async def get_activity(self, ref, full=False):
        from devcake.domain.model import Activity
        assert isinstance(ref, MissionRef)
        return Activity(mission=self.mission, entries=[])


def _mission(**kw):
    defaults = dict(
        instance="linear", pmo_id="p1", pmo_kind="issue", key="T-1",
        title="t", status="in_progress", labels={"DEVCAKE"}, repo="main",
        updated_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    return Mission(**defaults)


def _make_mgr(tmp_path, mission, forge=None):
    fake = _FeedFakePMO(mission)
    mgr = make_mission_manager(
        tmp_path, pmo=fake, forge=forge, config=AppConfig(),
        dev_types={"senior-dev": DevType(name="senior-dev",
                                         harness_template="claude-code")},
        messaging=NullMessaging(),
        noop_audit=True,
    )
    return mgr, fake, mgr.runs.store


# ── Slice A: instance-keyed give-up marks ────────────────────────────────────


def test_last_giveup_at_is_instance_scoped(tmp_path, monkeypatch):
    """Same bare pmo_id on two PMO instances must not share a watermark —
    the reader must key marks by (instance, pmo_id), matching feed._audit."""
    audit = tmp_path / "events.jsonl"
    monkeypatch.setattr(markers, "AUDIT_PATH", audit)
    markers._GIVEUP_STATE.update(path=None, offset=0, marks={})

    t_a = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)
    t_b = datetime(2026, 8, 18, 12, 5, 0, tzinfo=timezone.utc)
    lines = [
        {"ts": t_a.isoformat(), "instance": "a", "pmo_id": "42",
         "action": "devcake_failed", "detail": "EXECUTE"},
        {"ts": t_b.isoformat(), "instance": "b", "pmo_id": "42",
         "action": "devcake_failed", "detail": "EXECUTE"},
    ]
    audit.write_text("".join(json.dumps(e) + "\n" for e in lines))

    assert markers.last_giveup_at("42", instance="a") == t_a
    assert markers.last_giveup_at("42", instance="b") == t_b
    # bare / empty instance must not leak a named instance's mark
    assert markers.last_giveup_at("42", instance="") is None


def test_last_giveup_at_legacy_blank_instance_keys_as_empty(tmp_path, monkeypatch):
    """Pre-instance audit lines (missing instance) key as \"\" and stay
    invisible to a named-instance lookup."""
    audit = tmp_path / "events.jsonl"
    monkeypatch.setattr(markers, "AUDIT_PATH", audit)
    markers._GIVEUP_STATE.update(path=None, offset=0, marks={})

    t_legacy = datetime(2026, 8, 18, 11, 0, 0, tzinfo=timezone.utc)
    audit.write_text(json.dumps({
        "ts": t_legacy.isoformat(), "pmo_id": "99",
        "action": "devcake_failed", "detail": "PLAN",
    }) + "\n")

    assert markers.last_giveup_at("99", instance="") == t_legacy
    assert markers.last_giveup_at("99", instance="linear") is None


# ── Slice B: give-up feed carries docs/15 §3 detail ──────────────────────────


def test_give_up_feed_carries_last_error_class_message_and_run_id(
        tmp_path, monkeypatch):
    """docs/15 §3: give-up comment posts last error class + message + attempt
    count + a concrete final-attempt trace pointer (at least the run_id)."""
    m = _mission()
    mgr, fake, store = _make_mgr(tmp_path, m)
    mgr.config.max_attempts = 2
    mgr.config.attempt_reset = "label-ops"
    monkeypatch.delenv("OO_UI_URL", raising=False)

    t0 = datetime(2026, 8, 18, 14, 0, 0, tzinfo=timezone.utc)
    # two counted failures — the newest is the final attempt that tripped give-up
    for i, (cls, msg, rid) in enumerate((
        ("DEV_TIMEOUT", "first stall", "T-1-1-EXECUTE-AAAAAA"),
        ("DEV_BAD_OUTPUT", "fixture asserted wrong shape",
         "T-1-2-EXECUTE-BBBBBB"),
    ), start=1):
        r = Run(
            run_id=rid, mission_key="T-1", mission_pmo_id="p1",
            mission_type="EXECUTE", dev_type="senior-dev", seq=i,
            pmo_ref="linear", state="failed",
            error_class=cls, error=f"{cls}: {msg}",
            created_at=t0 + timedelta(seconds=i),
        )
        store.save(r)

    # attempt 3 = past max_attempts=2 → give up
    assert run_coro(dispatch._attempt_gate(
        mgr, m, MissionType.EXECUTE, 3)) is False

    assert any("DEVCAKE-FAILED" in str(add) for _rm, add in fake.swaps)
    body = next(c for c in fake.comments if "gave up" in c.lower())
    assert "DEV_BAD_OUTPUT" in body
    assert "fixture asserted wrong shape" in body
    assert "2" in body  # attempt count
    assert "T-1-2-EXECUTE-BBBBBB" in body
    # must not still use the vague mission.key-* glob alone
    assert "fixture asserted wrong shape" in body


def test_give_up_feed_includes_trace_id_when_oo_ui_configured(
        tmp_path, monkeypatch):
    """When OO_UI_URL is set and the final run has a traceparent, the feed
    names the derived 32-hex trace_id (same extraction as failure_record)."""
    m = _mission()
    mgr, fake, store = _make_mgr(tmp_path, m)
    mgr.config.max_attempts = 1
    monkeypatch.setenv("OO_UI_URL", "https://oo.example")

    # W3C traceparent: version-traceid-spanid-flags
    trace_id = "a" * 32
    tp = f"00-{trace_id}-{'b' * 16}-01"
    r = Run(
        run_id="T-1-1-EXECUTE-CCCCCC", mission_key="T-1", mission_pmo_id="p1",
        mission_type="EXECUTE", dev_type="senior-dev", seq=1,
        pmo_ref="linear", state="failed",
        error_class="DEV_BAD_OUTPUT",
        error="DEV_BAD_OUTPUT: missing result.json",
        traceparent=tp,
        created_at=datetime(2026, 8, 18, 15, 0, 0, tzinfo=timezone.utc),
    )
    store.save(r)

    assert run_coro(dispatch._attempt_gate(
        mgr, m, MissionType.EXECUTE, 2)) is False
    body = next(c for c in fake.comments if "gave up" in c.lower())
    assert "DEV_BAD_OUTPUT" in body
    assert "missing result.json" in body
    assert "T-1-1-EXECUTE-CCCCCC" in body
    assert trace_id in body
    # Test assertion on comment-body text — not URL sanitization.
    assert "https://oo.example" in body


# ── Slice C: out-of-pipeline-merge tripwire forge errors get loud ────────────


class _RaisingForge:
    """Forge whose PR lookup raises — tripwire must not stay silent."""

    capabilities = SimpleNamespace(mergeable_tristate=True)

    def __init__(self, exc: Exception):
        self._exc = exc

    async def get_pr_by_branch(self, branch):
        raise self._exc

    async def pr_state(self, pr_number):
        raise AssertionError("pr_state must not be reached after get_pr fails")


def test_out_of_pipeline_probe_failure_sets_anomaly_and_warns(
        tmp_path, caplog):
    """When get_pr_by_branch raises, log above DEBUG with exc_info and set
    mgr.anomalies naming detection failure — review flow still proceeds."""
    m = _mission(labels={"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = _RaisingForge(ForgeError("GET /pulls → network: timed out",
                                     status=None))
    mgr, _fake, _store = _make_mgr(tmp_path, m, forge=forge)
    run = Run(
        run_id="T-1-1-REVIEW-DDDDDD", mission_key="T-1", mission_pmo_id="p1",
        mission_type="REVIEW", dev_type="senior-dev", seq=1,
        pmo_ref="linear", repo_ref="main",
        stage_label_at_dispatch="DEVCAKE-REVIEW",
    )

    with caplog.at_level(logging.DEBUG, logger="devcake.missions"):
        run_coro(review._flag_out_of_pipeline_merge(mgr, run))

    anomaly = mgr.anomalies.get("p1")
    assert anomaly is not None
    assert "out-of-pipeline" in anomaly.lower()
    assert "detection" in anomaly.lower()
    # not the vanished-repo wording
    assert "no longer configured" not in anomaly
    # forge / probe failure named
    assert any(tok in anomaly.lower()
               for tok in ("forge", "probe", "failed", "error", "check"))

    warn_records = [r for r in caplog.records
                    if r.levelno >= logging.WARNING
                    and "out-of-pipeline" in r.getMessage().lower()]
    assert warn_records, "probe failure must log at WARNING or higher"
    assert any(r.exc_info for r in warn_records), "must include exc_info"
