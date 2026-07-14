"""ADR-0007: the Relations Mapper is advisory — the app validates every
proposed edge (unknown/self/terminal/duplicate/cycle are dropped) — plus the
MapperService cadence/degradation and the comment-provenance sentinel
classification the mapper's output relies on."""
import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from devcake.config import PMOInstance

from devcake.config import AppConfig, DevType
from devcake.domain.orchestrator import (MapperBusy, MapperService, MapperUnconfigured,
                              MissionManager)
from devcake.domain.model import Activity, ActivityEntry, AttachmentRef, Mission
from devcake.adapters.files.run_store import RunStore
from devcake.domain.run import Run

NOW = datetime.now(timezone.utc)


def m(pmo_id, key, status="backlog", blocked_by=()):
    return Mission(pmo_id=pmo_id, pmo_kind="issue", key=key, title=key,
                   status=status, labels={"DEVCAKE"}, updated_at=NOW,
                   blocked_by=list(blocked_by))


class MapPMO:
    def __init__(self, missions, activity=None):
        self.missions = missions
        self.activity = activity
        self.relations = []
        self.comments = []

    async def list_all(self, team_ref):
        return self.missions

    async def create_relation(self, blocker_id, blocked_id):
        self.relations.append((blocker_id, blocked_id))

    async def post_feed(self, ref, markdown):
        self.comments.append((ref.pmo_id, markdown))

    async def get_activity(self, ref):
        return self.activity


def make_mgr(tmp_path, pmo):
    mgr = MissionManager.__new__(MissionManager)
    mgr.instance_name = 'linear'
    mgr.instance = PMOInstance(name='linear', team_key='DEV')
    mgr.config = AppConfig()
    mgr.pmo = pmo
    mgr.runs = SimpleNamespace(store=RunStore(tmp_path / "runs"))
    mgr._grace, mgr._grace_next, mgr.breakers = set(), set(), {}
    mgr.merge_handoffs, mgr._merge_window_closed = {}, set()
    mgr._audit = lambda *a, **k: None
    return mgr


def run_coro(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_apply_mapper_edges_validates_everything(tmp_path):
    # B already blocked by A; C is terminal; D is free
    pmo = MapPMO([m("ia", "T-A"), m("ib", "T-B", blocked_by=["ia"]),
                  m("ic", "T-C", status="done"), m("id", "T-D")])
    mgr = make_mgr(tmp_path, pmo)
    created, rejected = run_coro(mgr._apply_mapper_edges([
        {"blocker": "T-X", "blocked": "T-A"},      # unknown key
        {"blocker": "T-A", "blocked": "T-A"},      # self-edge
        {"blocker": "T-C", "blocked": "T-D"},      # terminal blocker
        {"blocker": "T-A", "blocked": "T-B"},      # duplicate
        {"blocker": "T-B", "blocked": "T-A"},      # cycle (A already blocks B)
        {"blocker": "T-B", "blocked": "T-D"},      # legitimate
    ]))
    assert (created, rejected) == (1, 5)
    assert pmo.relations == [("ib", "id")]
    pmo_id, comment = pmo.comments[-1]
    assert pmo_id == "id" and "T-B" in comment
    assert comment.endswith("`devcake:v1`")        # notification is sentinel-signed


def test_creates_cycle_transitive():
    graph = {"a": {"b"}, "b": {"c"}}               # a depends on b depends on c
    assert MissionManager._creates_cycle(graph, "a", "c")       # c←a would loop
    assert not MissionManager._creates_cycle(graph, "c", "d")   # fresh edge is fine


def test_config_defaults():
    cfg = AppConfig()
    assert cfg.intake_paused is False
    assert cfg.relations_mapper.enabled is False           # manual-only by default
    assert cfg.relations_mapper.interval_minutes == 60
    assert cfg.relations_mapper.dev_type == "junior-dev"   # seeded cheap vehicle
    assert cfg.auto_resolve_merge_conflicts is True        # docs/03 §4.1
    assert cfg.merge_retry_window_minutes == 30
    # roundtrips through dump/validate (the /api/v1/config PUT path)
    assert AppConfig.model_validate(cfg.model_dump()) == cfg
    with pytest.raises(Exception):                         # ge=0 enforced
        AppConfig.model_validate({"merge_retry_window_minutes": -5})


def test_deep_merge_preserves_nested_siblings():
    from devcake.config import deep_merge
    base = AppConfig().model_dump()
    merged = deep_merge(base, {"relations_mapper": {"enabled": True}})
    assert merged["relations_mapper"]["enabled"] is True
    assert merged["relations_mapper"]["dev_type"] == "junior-dev"  # sibling survived


# ── MapperService cadence + degradation ──────────────────────────────────────

def make_service(tmp_path, enabled=True, dev_type="junior-dev"):
    cfg = AppConfig()
    cfg.relations_mapper.enabled = enabled
    cfg.relations_mapper.dev_type = dev_type
    mgr = make_mgr(tmp_path, MapPMO([]))
    dispatched = []

    async def fake_dispatch(dt, missions):
        dispatched.append(dt.name)
        return Run(run_id=f"TEAM-{len(dispatched)}-MAPPER-AAAAAA",
                   mission_key="TEAM", mission_type="MAPPER",
                   dev_type=dt.name, seq=len(dispatched))

    mgr.dispatch_mapper = fake_dispatch
    svc = MapperService(cfg, {"junior-dev": DevType(name="junior-dev",
                                                    harness_template="claude-code")},
                        mgr)
    return svc, mgr, dispatched


def mapper_run(n, state):
    return Run(run_id=f"TEAM-{n}-MAPPER-{'X' * 6}", mission_key="TEAM",
               mission_type="MAPPER", dev_type="junior-dev", seq=n, state=state)


def test_maybe_dispatch_respects_interval_and_toggle(tmp_path):
    svc, mgr, dispatched = make_service(tmp_path, enabled=False)
    svc._last_at = time.monotonic() - 10**6
    run_coro(svc.maybe_dispatch([]))
    assert dispatched == []                        # toggle off → never

    svc, mgr, dispatched = make_service(tmp_path)
    run_coro(svc.maybe_dispatch([]))
    assert dispatched == []                        # interval not elapsed
    svc._last_at = time.monotonic() - 10**6
    run_coro(svc.maybe_dispatch([]))
    assert dispatched == ["junior-dev"]            # elapsed → dispatched
    run_coro(svc.maybe_dispatch([]))
    assert dispatched == ["junior-dev"]            # watermark advanced


def test_watermark_not_advanced_on_dispatch_failure(tmp_path):
    svc, mgr, dispatched = make_service(tmp_path)

    async def boom(dt, missions):
        raise RuntimeError("dagu down")

    mgr.dispatch_mapper = boom
    svc._last_at = time.monotonic() - 10**6
    before = svc._last_at
    with pytest.raises(RuntimeError):
        run_coro(svc.maybe_dispatch([]))
    assert svc._last_at == before                  # next cycle retries


def test_degraded_after_three_dead_runs_skips_periodic_only(tmp_path):
    svc, mgr, dispatched = make_service(tmp_path)
    for i, st in enumerate(("failed", "timed_out", "failed"), start=1):
        mgr.runs.store.save(mapper_run(i, st))
    assert svc.degraded()
    svc._last_at = time.monotonic() - 10**6
    run_coro(svc.maybe_dispatch([]))
    assert dispatched == []                        # periodic backs off
    run = run_coro(svc.run_now())                  # manual is the reset signal
    assert run.mission_type == "MAPPER"
    # a finished run clears the condition (store-derived, restart-safe)
    mgr.runs.store.save(mapper_run(9, "finished"))
    assert svc.degraded() is None


def test_run_now_errors(tmp_path):
    svc, mgr, dispatched = make_service(tmp_path, dev_type=None)
    with pytest.raises(MapperUnconfigured):
        run_coro(svc.run_now())
    svc, mgr, dispatched = make_service(tmp_path)
    active = mapper_run(1, "running")
    mgr.runs.store.save(active)
    with pytest.raises(MapperBusy):
        run_coro(svc.run_now())


def test_activity_payload_marks_provenance(tmp_path):
    mission = m("i1", "T-1")
    entries = [
        ActivityEntry(ts=NOW, author="felipe", kind="comment",
                      body="🧾 DevCake transcript `2_EXECUTE.md` (run `x`)\n\n"
                           "stuff\n\n`devcake:v1`"),
        ActivityEntry(ts=NOW, author="felipe", kind="comment",
                      body="please use tabs, not spaces"),
    ]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    mgr = make_mgr(tmp_path, pmo)
    md = run_coro(mgr.activity_payload("i1"))["activity_md"]
    assert md.count("— 🤖 DevCake") == 1           # entry headers, not the legend
    assert md.count("— 🧑 HUMAN") == 1
    # same author on both entries — provenance came from the sentinel, not the name
    assert MissionManager._derive_seq(
        Activity(mission=mission, entries=entries)) == 3   # STEP_MARKER intact


def test_activity_payload_dedupes_colliding_filenames(tmp_path):
    # docs/07 §2 collision rule: same-second externalized entries get -2/-3
    # suffixes, and each index preview points at its OWN file
    mission = m("i1", "T-1")
    entries = [
        ActivityEntry(ts=NOW, author="a", kind="comment", body="first " + "x" * 3000),
        ActivityEntry(ts=NOW, author="b", kind="comment", body="second " + "y" * 3000),
    ]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    names = [a["filename"] for a in payload["attachments"]]
    base = f"entry-{NOW:%Y%m%dT%H%M%S}.md"
    assert names == [base, base.replace(".md", "-2.md")]
    md = payload["activity_md"]
    assert f"see: {base}" in md and f"see: {base.replace('.md', '-2.md')}" in md


def test_activity_payload_dedupes_downloaded_attachment_names(tmp_path):
    # a Linear attachment whose filename collides with an externalized entry
    mission = m("i1", "T-1")
    base = f"entry-{NOW:%Y%m%dT%H%M%S}.md"
    url = "https://uploads.linear.app/abc"
    entries = [
        ActivityEntry(ts=NOW, author="a", kind="comment", body="long " + "x" * 3000),
        ActivityEntry(ts=NOW, author="a", kind="comment",
                      body=f"file here: [{base}]({url})",
                      attachments=[AttachmentRef(url=url, name=base)]),
    ]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    pmo.download_asset = _returns(b"data")
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    names = [a["filename"] for a in payload["attachments"]]
    assert names == [base, base.replace(".md", "-2.md")]


def _returns(value):
    async def _f(*a, **k):
        return value
    return _f
