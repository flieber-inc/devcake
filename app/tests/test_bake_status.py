"""Host baker liveness: heartbeat on /data, observed by the app.

Public seam: baker_liveness(status, *, now) → {alive, reason}.
Independent expected ages are the literals 5s (alive) and 31s (dead).
A baking status without a heartbeat is dead — that is a crash mid-bake,
not a live compile.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)


def test_missing_heartbeat_is_dead():
    from devcake.bake_status import baker_liveness

    live = baker_liveness({"state": "baking", "jobs": []}, now=NOW)
    assert live["alive"] is False
    assert "not checked in" in live["reason"]


def test_fresh_heartbeat_is_alive_even_while_baking():
    from devcake.bake_status import baker_liveness

    ts = (NOW - timedelta(seconds=5)).isoformat()
    live = baker_liveness(
        {"state": "baking", "heartbeat_at": ts, "jobs": []}, now=NOW)
    assert live["alive"] is True
    assert live["reason"] == ""


def test_stale_heartbeat_is_dead():
    from devcake.bake_status import HEARTBEAT_STALE_SECONDS, baker_liveness

    assert HEARTBEAT_STALE_SECONDS == 30
    ts = (NOW - timedelta(seconds=31)).isoformat()
    live = baker_liveness(
        {"state": "ready", "heartbeat_at": ts}, now=NOW)
    assert live["alive"] is False
    assert "31" in live["reason"] or "checked in" in live["reason"]


def test_drain_baker_log_returns_only_new_lines(tmp_path, monkeypatch):
    from devcake import bake_status as bs

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    log = tmp_path / bs.BAKER_LOG_NAME
    log.write_text('{"event":"tick"}\n{"event":"error"}\n')
    first = bs.drain_baker_log(root=tmp_path)
    assert [r["event"] for r in first] == ["tick", "error"]
    again = bs.drain_baker_log(root=tmp_path)
    assert again == []
    with log.open("a") as fh:
        fh.write('{"event":"down"}\n')
    third = bs.drain_baker_log(root=tmp_path)
    assert [r["event"] for r in third] == ["down"]


def test_baker_transition_fires_on_edges_only():
    from devcake.bake_status import baker_transition

    assert baker_transition(None, False) == "dead"
    assert baker_transition(True, False) == "dead"
    assert baker_transition(False, True) == "alive"
    assert baker_transition(True, True) is None
    assert baker_transition(False, False) is None
