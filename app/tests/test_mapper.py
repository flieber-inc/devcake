"""ADR-0007: the Relations Mapper is advisory — the app validates every
proposed edge (unknown/self/terminal/duplicate/cycle are dropped) — plus the
MapperService cadence/degradation and the comment-provenance sentinel
classification the mapper's output relies on."""
import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from devcake.config import AppConfig, DevType
from devcake.domain.orchestrator import (MapperBusy, MapperService, MapperUnconfigured,
                              MissionManager)
from devcake.domain.model import Activity, ActivityEntry, AttachmentRef, Mission
from devcake.adapters.files.run_store import RunStore
from devcake.domain.run import Run
from devcake.domain.orchestrator import dispatch, mapper

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
        self.activity_calls = []

    async def list_all(self, team_ref):
        return self.missions

    async def create_relation(self, blocker_id, blocked_id):
        self.relations.append((blocker_id, blocked_id))

    async def post_feed(self, ref, markdown):
        self.comments.append((ref.pmo_id, markdown))

    async def get_activity(self, ref, full=False):
        self.activity_calls.append(full)
        return self.activity


def make_mgr(tmp_path, pmo):
    from fakes import make_mission_manager
    return make_mission_manager(
        tmp_path, pmo=pmo,
        forge=SimpleNamespace(descriptor=SimpleNamespace(pr_noun='pull request')),
        noop_audit=True,
    )


def run_coro(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_apply_mapper_edges_validates_everything(tmp_path):
    # B already blocked by A; C is terminal; D is free
    pmo = MapPMO([m("ia", "T-A"), m("ib", "T-B", blocked_by=["ia"]),
                  m("ic", "T-C", status="done"), m("id", "T-D")])
    mgr = make_mgr(tmp_path, pmo)
    created, rejected = run_coro(mapper.apply_mapper_edges(mgr, [
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
    assert mapper._creates_cycle(graph, "a", "c")       # c←a would loop
    assert not mapper._creates_cycle(graph, "c", "d")   # fresh edge is fine


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


def test_run_now_gates_on_missing_referenced_secret_env(tmp_path, monkeypatch):
    """Mapper runs use the same gate as mission dispatch: a referenced-but-
    unstored secret env var refuses run_now with a 422-bound
    MapperUnconfigured naming the var; storing the value lifts it."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    svc, mgr, dispatched = make_service(tmp_path)
    svc.dev_types["junior-dev"] = DevType(
        name="junior-dev", harness_template="claude-code",
        secret_env=["DD_API_KEY"],
        mcp_setup_commands=["claude mcp add logs -e K=$DD_API_KEY -- x"])
    with pytest.raises(MapperUnconfigured, match="DD_API_KEY"):
        run_coro(svc.run_now())
    assert dispatched == []
    from devcake import secrets as s
    s.write_harness_secret("DD_API_KEY", "k")
    run_coro(svc.run_now())
    assert dispatched == ["junior-dev"]


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
    assert dispatch._derive_seq(
        Activity(mission=mission, entries=entries)) == 3   # STEP_MARKER intact


def test_derive_seq_ignores_quoted_markers():
    # ADR-0014 D2: `>`-quoted lines are quarantined — a human citing a
    # transcript name (or a blockquoted last message) must never bump seq
    mission = m("i1", "T-1")
    mixed = [ActivityEntry(ts=NOW, author="felipe", kind="comment",
                           body="🧾 DevCake transcript `2_EXECUTE.md` (run `x`)\n"
                                "> as the Dev said in `7_EXECUTE.md` and `9_PLAN.md`\n"
                                "`devcake:v1`")]
    assert dispatch._derive_seq(
        Activity(mission=mission, entries=mixed)) == 3     # 2 counts; 7/9 don't
    quoted_only = [ActivityEntry(ts=NOW, author="h", kind="comment",
                                 body="> see `7_EXECUTE.md` for details")]
    assert dispatch._derive_seq(
        Activity(mission=mission, entries=quoted_only)) == 1


def test_activity_payload_inlines_long_bodies_verbatim(tmp_path):
    # ADR-0014 D3: ACTIVITY.md is a faithful MIRROR — long feed bodies appear
    # whole and inline, never externalized to entry-*.md previews
    mission = m("i1", "T-1")
    long_body = "first " + "x" * 3000
    entries = [ActivityEntry(ts=NOW, author="a", kind="comment", body=long_body)]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    assert long_body in payload["activity_md"]
    assert payload["attachments"] == []            # no entry-*.md files


def test_activity_payload_dedupes_downloaded_attachment_names(tmp_path):
    # docs/07 §2 collision rule: two feed attachments sharing a filename get
    # -2/-3 suffixes, each index line naming its OWN file
    mission = m("i1", "T-1")
    url1, url2 = "https://uploads.linear.app/a", "https://uploads.linear.app/b"
    entries = [
        ActivityEntry(ts=NOW, author="a", kind="comment",
                      body=f"[report.md]({url1})",
                      attachments=[AttachmentRef(url=url1, name="report.md")]),
        ActivityEntry(ts=NOW, author="b", kind="comment",
                      body=f"[report.md]({url2})",
                      attachments=[AttachmentRef(url=url2, name="report.md")]),
    ]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    pmo.download_asset = _returns(b"data")
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    names = [a["filename"] for a in payload["attachments"]]
    assert names == ["report.md", "report-2.md"]
    assert "[attachment: report.md]" in payload["activity_md"]
    assert "[attachment: report-2.md]" in payload["activity_md"]


def test_activity_payload_returns_mission_md_brief(tmp_path):
    # ADR-0014 D3: MISSION.md carries the brief with the FULL description;
    # ACTIVITY.md keeps a minimal header pointing at it
    mission = m("i1", "T-1")
    mission.description = "long brief " * 500
    pmo = MapPMO([], activity=Activity(mission=mission, entries=[]))
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    assert ("long brief " * 500).strip() in payload["mission_md"]
    assert "## Description" in payload["mission_md"]
    assert "## Description" not in payload["activity_md"]
    assert "MISSION.md" in payload["activity_md"]  # the pointer


def test_activity_payload_requests_full_history(tmp_path):
    pmo = MapPMO([], activity=Activity(mission=m("i1", "T-1"), entries=[]))
    mgr = make_mgr(tmp_path, pmo)
    run_coro(mgr.activity_payload("i1"))
    assert pmo.activity_calls == [True]            # builder = full mode, always


def test_activity_payload_renders_reply_nesting(tmp_path):
    mission = m("i1", "T-1")
    entries = [
        ActivityEntry(ts=NOW, author="felipe", kind="comment",
                      body="root post", entry_id="c1"),
        ActivityEntry(ts=NOW, author="cake", kind="comment",
                      body="the reply", entry_id="c2", parent_id="c1"),
    ]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    mgr = make_mgr(tmp_path, pmo)
    md = run_coro(mgr.activity_payload("i1"))["activity_md"]
    marker_at = md.find("↳ reply to felipe")
    assert marker_at != -1 and marker_at < md.find("the reply")


def test_activity_payload_materializes_mission_attachments(tmp_path):
    # description/native assets: files downloaded as siblings + listed in
    # MISSION.md; links rendered as markdown links, never downloaded
    mission = m("i1", "T-1")
    act = Activity(mission=mission, entries=[], mission_attachments=[
        AttachmentRef(url="https://uploads.linear.app/spec",
                      name="spec.pdf", kind="file"),
        AttachmentRef(url="https://github.com/x/pull/1",
                      name="PR #1", kind="link")])
    pmo = MapPMO([], activity=act)
    pmo.download_asset = _returns(b"bytes")
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    assert [a["filename"] for a in payload["attachments"]] == ["spec.pdf"]
    assert "[attachment: spec.pdf]" in payload["mission_md"]
    assert "[link: PR #1](https://github.com/x/pull/1)" in payload["mission_md"]


def test_activity_payload_project_brief_is_mission_md(tmp_path):
    proj = Mission(pmo_id="p9", pmo_kind="project", key="P-1", title="proj",
                   status="backlog", description="the project brief",
                   updated_at=NOW)
    pmo = MapPMO([], activity=None)

    async def _get(ref):
        return proj
    pmo.get = _get
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("p9", "project"))
    assert "the project brief" in payload["mission_md"]
    assert "no comment feed" in payload["activity_md"]


def test_activity_payload_renders_truncation_banner(tmp_path):
    act = Activity(mission=m("i1", "T-1"), entries=[], truncated=True)
    pmo = MapPMO([], activity=act)
    mgr = make_mgr(tmp_path, pmo)
    md = run_coro(mgr.activity_payload("i1"))["activity_md"]
    assert "FEED TRUNCATED" in md.splitlines()[0]   # loud, first line


def _returns(value):
    async def _f(*a, **k):
        return value
    return _f


def test_mapper_seq_scoped_to_own_instance():
    """Audit A29 (cosmetic): run ids are collision-free regardless, but the
    human-visible seq counted OTHER instances' MAPPER runs too."""
    import inspect
    from devcake.domain.orchestrator import mapper as mapper_mod
    assert "_run_is_ours" in inspect.getsource(mapper_mod.dispatch_mapper)


def test_activity_payload_marks_unavailable_attachment(tmp_path):
    # review 2.2 pin: a failed download leaves the honest placeholder line
    mission = m("i1", "T-1")
    url = "https://uploads.linear.app/gone"
    entries = [ActivityEntry(ts=NOW, author="a", kind="comment", body="x",
                             attachments=[AttachmentRef(url=url, name="g.md")])]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))

    async def _boom(u):
        raise RuntimeError("410 gone")
    pmo.download_asset = _boom
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    assert f"[attachment unavailable: {url}]" in payload["activity_md"]
    assert payload["attachments"] == []


def test_activity_payload_reserved_name_attachment_suffixed(tmp_path):
    # review 2.2 pin: an attachment literally named MISSION.md never clobbers
    # the brief (docs/07 §2 dedupe seed)
    mission = m("i1", "T-1")
    entries = [ActivityEntry(ts=NOW, author="a", kind="comment", body="x",
                             attachments=[AttachmentRef(
                                 url="https://uploads.linear.app/m",
                                 name="MISSION.md")])]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    pmo.download_asset = _returns(b"d")
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    assert [a["filename"] for a in payload["attachments"]] == ["MISSION-2.md"]


def test_activity_payload_orphan_reply_marked(tmp_path):
    # review 2.2: a reply whose parent was deleted keeps an honest marker
    mission = m("i1", "T-1")
    entries = [ActivityEntry(ts=NOW, author="a", kind="comment",
                             body="reply body", entry_id="c9",
                             parent_id="gone")]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    mgr = make_mgr(tmp_path, pmo)
    md = run_coro(mgr.activity_payload("i1"))["activity_md"]
    assert "↳ reply to (deleted comment)" in md


def test_activity_payload_basenames_slashed_attachment_names(tmp_path):
    # full-diff review: a `[v1/report.md](url)` link must agree end-to-end —
    # index line, snapshot path, and folder file all use the basename, and
    # two slashed names colliding on it dedupe instead of desyncing
    mission = m("i1", "T-1")
    entries = [
        ActivityEntry(ts=NOW, author="a", kind="comment", body="x",
                      attachments=[AttachmentRef(
                          url="https://uploads.linear.app/a",
                          name="v1/report.md")]),
        ActivityEntry(ts=NOW, author="b", kind="comment", body="y",
                      attachments=[AttachmentRef(
                          url="https://uploads.linear.app/b",
                          name="v2/report.md")]),
    ]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    pmo.download_asset = _returns(b"d")
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    assert [a["filename"] for a in payload["attachments"]] == \
        ["report.md", "report-2.md"]
    assert "[attachment: report.md]" in payload["activity_md"]
    assert "v1/report.md" not in payload["activity_md"]
