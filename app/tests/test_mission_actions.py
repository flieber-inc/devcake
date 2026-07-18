"""Mission-action application service (docs/05 §1; TDD-first).

Exercises the new driving-adapter-facing seam that the admin UI writes through:
label actions, steering, create-mission, stop-run. No FastAPI TestClient —
tests call the module directly with per-file fakes, mirroring `test_pmo_contract`
and the `clear.py`-style DI.

Rules under test:
- INV-1 preserved: Linear is the source of truth (labels + comment).
- Steering comments must NOT carry the DevCake sentinel — else they classify as
  our own record (`_is_devcake_comment`) and the attempt-counter reset misses.
- 409 preconditions decided by the CACHED labels; the port swap raising
  RuntimeError is a 502, not a 409.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from devcake.api import mission_actions as ma
from devcake.domain.model import MissionRef
from devcake.domain.orchestrator.markers import COMMENT_SENTINEL


# ── async helper (mirrors test_pmo_contract) ────────────────────────────────

def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── minimal fakes (ISP: implement only the port surface exercised) ──────────

class FakePMO:
    """Records swap_labels / post_feed / create_mission calls; raises on demand."""

    def __init__(self, *, swap_raises: Exception | None = None,
                 post_feed_raises: Exception | None = None):
        self.swaps: list[tuple[MissionRef, set, set]] = []
        self.feeds: list[tuple[MissionRef, str]] = []
        self.creates: list[tuple] = []
        self._swap_raises = swap_raises
        self._post_feed_raises = post_feed_raises
        self.next_key = "T-1"
        self.next_pmo_id = "pmo-created-1"

    async def swap_labels(self, ref: MissionRef, remove: set[str], add: set[str]) -> None:
        assert isinstance(ref, MissionRef)
        if self._swap_raises is not None:
            raise self._swap_raises
        self.swaps.append((ref, set(remove), set(add)))

    async def post_feed(self, ref: MissionRef, markdown: str) -> None:
        assert isinstance(ref, MissionRef)
        if self._post_feed_raises is not None:
            raise self._post_feed_raises
        self.feeds.append((ref, markdown))

    async def create_mission(self, team_ref: str, title: str, description: str,
                             priority: str, label_names: set[str],
                             parent_ref: str | None = None) -> tuple[str, str]:
        self.creates.append((team_ref, title, description, priority,
                             set(label_names), parent_ref))
        return self.next_key, self.next_pmo_id


class FakeMgr:
    """Duck-typed MissionManager: only the surface `mission_actions` needs."""

    def __init__(self, *, name: str = "linear", team_key: str = "DEV",
                 pmo: FakePMO | None = None):
        self.instance = SimpleNamespace(name=name, team_key=team_key)
        self.instance_name = name
        self.pmo = pmo if pmo is not None else FakePMO()
        self.audits: list[tuple[str, str, str]] = []

    def _audit(self, pmo_id: str, action: str, detail: str = "") -> None:
        self.audits.append((pmo_id, action, detail))


class FakeStore:
    def __init__(self, runs: dict[str, Any] | None = None):
        self._runs = runs or {}

    def get(self, run_id: str):
        return self._runs.get(run_id)


class FakeRunManager:
    def __init__(self):
        self.kills: list[tuple[Any, str, str]] = []

    async def kill(self, run, new_state, reason):
        self.kills.append((run, new_state, reason))


# ── fixture-y helpers ────────────────────────────────────────────────────────

def _row(pmo_id="pmo-1", instance="linear", kind="issue",
         labels: set[str] | None = None, team="DEV", key="DEV-1"):
    return {
        "instance": instance, "team": team, "key": key, "kind": kind,
        "title": "t", "status": "in_progress", "priority": "medium",
        "labels": sorted(labels or set()),
        "mission_type": None, "schedulable": False,
        "repo": None, "reason": "", "blocked_by": [],
        "pmo_id": pmo_id,
    }


def _fresh_env(row_labels: set[str] | None = None,
               *, pmo: FakePMO | None = None) -> tuple[list[dict], dict[str, FakeMgr]]:
    mgr = FakeMgr(pmo=pmo)
    row = _row(labels=row_labels)
    return [row], {"linear": mgr}


# ── 1) label-action preconditions & success ─────────────────────────────────

def test_retry_when_failed_absent_returns_409():
    cache, managers = _fresh_env(row_labels=set())
    with pytest.raises(HTTPException) as exc:
        run(ma.label_action("pmo-1", "retry",
                            missions_cache=cache, managers=managers))
    assert exc.value.status_code == 409
    assert not managers["linear"].pmo.swaps  # port never called


def test_retry_when_failed_present_swaps_and_returns_projection():
    cache, managers = _fresh_env(row_labels={"DEVCAKE", "DEVCAKE-FAILED"})
    result = run(ma.label_action("pmo-1", "retry",
                                 missions_cache=cache, managers=managers))
    swaps = managers["linear"].pmo.swaps
    assert len(swaps) == 1
    ref, remove, add = swaps[0]
    assert ref == MissionRef("pmo-1", "issue")
    assert remove == {"DEVCAKE-FAILED"} and add == set()
    assert result["labels"] == ["DEVCAKE"]  # projected, sorted
    audits = managers["linear"].audits
    assert audits and audits[0][1] == "ui_retry"


@pytest.mark.parametrize("action,initial,expected_labels", [
    ("park",   {"DEVCAKE"},                        ["DEVCAKE", "DEVCAKE-SKIP"]),
    ("unpark", {"DEVCAKE", "DEVCAKE-SKIP"},        ["DEVCAKE"]),
    ("resume", {"DEVCAKE", "DEVCAKE-NEEDS-HUMAN"}, ["DEVCAKE"]),
])
def test_action_success_paths_projection_and_audit(action, initial, expected_labels):
    cache, managers = _fresh_env(row_labels=initial)
    result = run(ma.label_action("pmo-1", action,
                                 missions_cache=cache, managers=managers))
    assert result["labels"] == expected_labels
    assert managers["linear"].pmo.swaps, "port was not called"
    audits = managers["linear"].audits
    assert audits and audits[0][1] == f"ui_{action}"


@pytest.mark.parametrize("action,initial", [
    ("park",   {"DEVCAKE-SKIP"}),         # already parked
    ("unpark", set()),                     # not parked
    ("resume", set()),                     # nothing to resume
])
def test_action_precondition_failure_paths_return_409(action, initial):
    cache, managers = _fresh_env(row_labels=initial)
    with pytest.raises(HTTPException) as exc:
        run(ma.label_action("pmo-1", action,
                            missions_cache=cache, managers=managers))
    assert exc.value.status_code == 409
    assert not managers["linear"].pmo.swaps


def test_unknown_action_returns_422():
    cache, managers = _fresh_env(row_labels={"DEVCAKE"})
    with pytest.raises(HTTPException) as exc:
        run(ma.label_action("pmo-1", "yolo",
                            missions_cache=cache, managers=managers))
    assert exc.value.status_code == 422


def test_unknown_pmo_id_returns_404():
    cache, managers = _fresh_env(row_labels={"DEVCAKE"})
    with pytest.raises(HTTPException) as exc:
        run(ma.label_action("ghost", "park",
                            missions_cache=cache, managers=managers))
    assert exc.value.status_code == 404


def test_stale_instance_returns_409():
    cache, _ = _fresh_env(row_labels={"DEVCAKE"})
    with pytest.raises(HTTPException) as exc:
        run(ma.label_action("pmo-1", "park",
                            missions_cache=cache, managers={}))  # instance gone
    assert exc.value.status_code == 409


def test_swap_labels_runtime_error_bubbles_as_502():
    pmo = FakePMO(swap_raises=RuntimeError("DEVCAKE-FAILED label missing from team"))
    cache, managers = _fresh_env(row_labels={"DEVCAKE-FAILED"}, pmo=pmo)
    with pytest.raises(HTTPException) as exc:
        run(ma.label_action("pmo-1", "retry",
                            missions_cache=cache, managers=managers))
    assert exc.value.status_code == 502


# ── 2) steering / comment endpoint ──────────────────────────────────────────

def test_comment_blank_body_returns_422():
    cache, managers = _fresh_env(row_labels={"DEVCAKE"})
    with pytest.raises(HTTPException) as exc:
        run(ma.post_steering("pmo-1", "   ",
                             missions_cache=cache, managers=managers))
    assert exc.value.status_code == 422


def test_comment_calls_post_feed_without_sentinel_and_redacts():
    cache, managers = _fresh_env(row_labels={"DEVCAKE"})
    # secret registered so `redact` masks it
    from devcake import security
    security.register_runtime_secret("__test_mission_actions", "supersecrettoken-xyzabc")
    try:
        run(ma.post_steering("pmo-1",
                             "please retry with token supersecrettoken-xyzabc",
                             missions_cache=cache, managers=managers))
    finally:
        security.unregister_runtime_secret("__test_mission_actions")
    feeds = managers["linear"].pmo.feeds
    assert len(feeds) == 1
    ref, md = feeds[0]
    assert ref == MissionRef("pmo-1", "issue")
    assert COMMENT_SENTINEL not in md, \
        "steering comments must never carry the DevCake sentinel"
    assert "supersecrettoken-xyzabc" not in md, "raw secret must be redacted"


def test_comment_unknown_pmo_id_returns_404():
    cache, managers = _fresh_env(row_labels={"DEVCAKE"})
    with pytest.raises(HTTPException) as exc:
        run(ma.post_steering("ghost", "hello",
                             missions_cache=cache, managers=managers))
    assert exc.value.status_code == 404


# ── 3) create-mission endpoint ──────────────────────────────────────────────

def test_create_mission_blank_title_returns_422():
    mgr = FakeMgr()
    with pytest.raises(HTTPException) as exc:
        run(ma.create_mission(title="  ", description="", priority="medium",
                              instance_name=None,
                              managers={"linear": mgr},
                              team_keys={"linear": "DEV"}))
    assert exc.value.status_code == 422


def test_create_mission_defaults_to_first_instance_and_passes_devcake_label():
    mgr = FakeMgr()
    result = run(ma.create_mission(
        title="Ship the thing", description="details",
        priority="high", instance_name=None,
        managers={"linear": mgr},
        team_keys={"linear": "DEV"}))
    assert result == {"key": "T-1", "pmo_id": "pmo-created-1", "instance": "linear"}
    calls = mgr.pmo.creates
    assert len(calls) == 1
    team_ref, title, description, priority, labels, parent = calls[0]
    assert team_ref == "DEV"                          # passed positionally
    assert title == "Ship the thing"
    assert description == "details"
    assert priority == "high"
    assert labels == {"DEVCAKE"}                      # ONBOARD triage entry
    assert parent is None


def test_create_mission_no_instance_configured_returns_409():
    with pytest.raises(HTTPException) as exc:
        run(ma.create_mission(title="ok", description="", priority="medium",
                              instance_name=None, managers={}, team_keys={}))
    assert exc.value.status_code == 409


def test_create_mission_explicit_unknown_instance_returns_409():
    mgr = FakeMgr()
    with pytest.raises(HTTPException) as exc:
        run(ma.create_mission(title="ok", description="", priority="medium",
                              instance_name="nope",
                              managers={"linear": mgr},
                              team_keys={"linear": "DEV"}))
    assert exc.value.status_code == 409


class _RaisingPMO(FakePMO):
    async def create_mission(self, *args, **kwargs):
        raise RuntimeError("linear graphql: authentication error")


def test_create_mission_pmo_runtime_error_returns_502():
    mgr = FakeMgr(pmo=_RaisingPMO())
    with pytest.raises(HTTPException) as exc:
        run(ma.create_mission(title="ok", description="", priority="medium",
                              instance_name=None,
                              managers={"linear": mgr},
                              team_keys={"linear": "DEV"}))
    assert exc.value.status_code == 502


class _FeedRaisingPMO(FakePMO):
    async def post_feed(self, ref, markdown):
        raise RuntimeError("linear graphql: authentication error")


def test_post_steering_pmo_runtime_error_returns_502():
    cache, _ = _fresh_env(row_labels={"DEVCAKE"})
    managers = {"linear": FakeMgr(pmo=_FeedRaisingPMO())}
    with pytest.raises(HTTPException) as exc:
        run(ma.post_steering("pmo-1", "hello",
                             missions_cache=cache, managers=managers))
    assert exc.value.status_code == 502


# ── 4) stop-run endpoint ────────────────────────────────────────────────────

def _run_record(state="running"):
    return SimpleNamespace(run_id="r-1", state=state)


def test_stop_run_unknown_returns_404():
    store = FakeStore()
    with pytest.raises(HTTPException) as exc:
        run(ma.stop_run("ghost", run_manager=FakeRunManager(), run_store=store))
    assert exc.value.status_code == 404


@pytest.mark.parametrize("state", ["finished", "failed", "timed_out", "orphaned"])
def test_stop_run_terminal_returns_409(state):
    store = FakeStore({"r-1": _run_record(state)})
    with pytest.raises(HTTPException) as exc:
        run(ma.stop_run("r-1", run_manager=FakeRunManager(), run_store=store))
    assert exc.value.status_code == 409


@pytest.mark.parametrize("state", ["dispatched", "running", "finalizing"])
def test_stop_run_live_calls_kill_with_failed_and_operator_reason(state):
    rec = _run_record(state)
    store = FakeStore({"r-1": rec})
    rm = FakeRunManager()
    result = run(ma.stop_run("r-1", run_manager=rm, run_store=store))
    assert rm.kills, "RunManager.kill was not called"
    killed_run, new_state, reason = rm.kills[0]
    assert killed_run is rec
    assert new_state == "failed"
    assert "operator" in reason.lower()  # honest audit trail per plan
    assert result["state"] == "failed"
