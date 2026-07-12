"""ADR-0007: the Relations Mapper is advisory — the app validates every
proposed edge (unknown/self/terminal/duplicate/cycle are dropped) — plus the
comment-provenance sentinel classification the mapper's output relies on."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from devcake.config import AppConfig
from devcake.missions import MissionManager
from devcake.pmo import Activity, ActivityEntry, Mission
from devcake.state import RunStore

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

    async def post_comment(self, pmo_id, md):
        self.comments.append((pmo_id, md))

    async def get_activity(self, pmo_id):
        return self.activity


def make_mgr(tmp_path, pmo):
    mgr = MissionManager.__new__(MissionManager)
    mgr.config = AppConfig()
    mgr.pmo = pmo
    mgr.runs = SimpleNamespace(store=RunStore(tmp_path / "runs"))
    mgr._grace, mgr._grace_next, mgr.breakers = set(), set(), {}
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
    assert cfg.relations_mapper.enabled is False
    assert cfg.relations_mapper.interval_minutes == 60
    assert cfg.relations_mapper.dev_type is None
    # roundtrips through dump/validate (the /api/v1/config PUT path)
    assert AppConfig.model_validate(cfg.model_dump()) == cfg


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
