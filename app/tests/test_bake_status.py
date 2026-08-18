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


def test_drain_outbox_deletes_the_file_after_reading(tmp_path, monkeypatch):
    """Baker→app mailbox: a complete outbox file is claimed and deleted."""
    from devcake import bake_status as bs

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    box = tmp_path / bs.OUTBOX_DIR
    box.mkdir()
    dest = box / "tick-1.jsonl"
    dest.write_text('{"event":"tick","state":"ready"}\n')
    first = bs.drain_baker_log(root=tmp_path)
    assert [r.get("event") for r in first] == ["tick"]
    assert not dest.is_file()
    assert bs.drain_baker_log(root=tmp_path) == []


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


def test_prune_request_writes_timestamp_and_no_image_names(tmp_path, monkeypatch):
    from devcake import bake_status as bs

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    when = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
    out = bs.request_prune(root=tmp_path)
    assert out == {"ok": True, "requested": True}
    dest = tmp_path / bs.PRUNE_REQUEST_NAME
    body = dest.read_text()
    assert "devcake/dev-" not in body
    assert "nginx" not in body
    rec = __import__("json").loads(body)
    assert "requested_at" in rec


def test_prune_request_also_drops_a_fresh_keep_set_order(tmp_path, monkeypatch):
    """Prune click is two inboxes: keep-set (desired pins) + prune request."""
    from devcake import bake_status as bs
    from devcake.config import DevType

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    bs.request_prune(
        root=tmp_path,
        dev_types={"d": DevType(name="d", harness_template="grok-build")})
    keep = tmp_path / "harness_keep_set.json"
    assert keep.is_file()
    body = __import__("json").loads(keep.read_text())
    assert body["pins"][0]["template"] == "grok-build"
    assert (tmp_path / bs.PRUNE_REQUEST_NAME).is_file()


def test_harness_prune_http_route_returns_ok(tmp_path, monkeypatch):
    """POST /api/v1/harness/prune must read Dev Types from services, not AppConfig.

    Regression: the route used svc().config.dev_types (AttributeError → 500).
    Dev Types live on Services as svc().dev_types.
    """
    import json

    import pytest
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from fakes import make_services
    from devcake import bake_status as bs
    from devcake.api import main as app_main
    from devcake.config import AppConfig, DevType

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    dts = {"d": DevType(name="d", harness_template="grok-build")}
    monkeypatch.setattr(app_main, "services", make_services(
        config=AppConfig(), dev_types=dts))

    probe = FastAPI()
    probe.add_api_route(
        "/api/v1/harness/prune", app_main.request_harness_prune, methods=["POST"])
    response = TestClient(probe).post("/api/v1/harness/prune")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "requested": True}
    assert (tmp_path / bs.PRUNE_REQUEST_NAME).is_file()
    keep = tmp_path / "harness_keep_set.json"
    assert keep.is_file()
    assert json.loads(keep.read_text())["pins"][0]["template"] == "grok-build"


def test_baker_transition_fires_on_edges_only():
    from devcake.bake_status import baker_transition

    assert baker_transition(None, False) == "dead"
    assert baker_transition(True, False) == "dead"
    assert baker_transition(False, True) == "alive"
    assert baker_transition(True, True) is None
    assert baker_transition(False, False) is None
