"""CAKE-124: offer every decomposition ancestor's activity under
activity/upstream/{MISSION-KEY}/ (directed parent_ref chain to root).

Public seams under test:
- family_graph.decomposition_ancestors
- activity_payload.activity_payload (upstream files + gaps + truncation)
- dispatch gate via activity_payload upstream_gaps + context_sourcing_strict
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone

from devcake.domain.model import Activity, ActivityEntry, AttachmentRef, Mission
from devcake.domain.orchestrator import family_graph
from devcake.domain.orchestrator.markers import LABEL_CREATED
from test_steward import MapPMO, make_mgr, run_coro

NOW = datetime.now(timezone.utc)
MANIFEST = "ab" * 32


def _marker(parent: str, depth: int) -> str:
    return (f"`devcake:decomposition:v1 parent={parent} "
            f"manifest={MANIFEST} part=1/2 depth={depth}`")


def _m(pmo_id: str, key: str, *, parent: str | None = None, depth: int = 1,
       labels: set | None = None, status: str = "backlog") -> Mission:
    labs = set(labels) if labels is not None else {"DEVCAKE"}
    desc = f"brief for {key}"
    if parent is not None:
        labs.add(LABEL_CREATED)
        desc += "\n\n" + _marker(parent, depth)
    return Mission(
        instance="linear", pmo_id=pmo_id, pmo_kind="issue", key=key, title=key,
        status=status, labels=labs, updated_at=NOW, description=desc,
    )


def test_decomposition_ancestors_three_level_nearest_first():
    """Root → child → grandchild: ancestors are [child, root] (nearest first)."""
    root = _m("r", "ROOT-1")
    child = _m("c", "CHILD-1", parent="r", depth=1)
    grand = _m("g", "GRAND-1", parent="c", depth=2)
    got = family_graph.decomposition_ancestors(grand, [root, child, grand])
    assert [m.key for m in got] == ["CHILD-1", "ROOT-1"]


def test_decomposition_ancestors_stops_on_cycle():
    """A forged/corrupt parent cycle must not loop forever."""
    a = _m("a", "A-1", parent="b", depth=1)
    b = _m("b", "B-1", parent="a", depth=1)
    got = family_graph.decomposition_ancestors(a, [a, b])
    assert [m.key for m in got] == ["B-1"]


def test_decomposition_ancestors_ignores_blocked_by_only_neighbors():
    """blocked_by siblings are NOT ancestors — ADR-0017 stays separate."""
    root = _m("r", "ROOT-1")
    child = _m("c", "CHILD-1", parent="r", depth=1)
    sibling = _m("s", "SIB-1", parent="r", depth=1)
    sibling.blocked_by = ["c"]
    child.blocked_by = ["s"]
    got = family_graph.decomposition_ancestors(child, [root, child, sibling])
    assert [m.key for m in got] == ["ROOT-1"]


class MultiActivityPMO(MapPMO):
    """MapPMO that serves per-mission activity from a dict keyed by pmo_id."""

    def __init__(self, missions, activities: dict[str, Activity], **kw):
        super().__init__(missions, **kw)
        self.activities = activities
        self.fail_ids: set[str] = set()
        self.get_calls: list[str] = []
        self.statuses: list[str] = []
        self.swaps: list = []

    def _find(self, ref):
        for m in self.missions:
            if m.pmo_id == ref.pmo_id or (
                    m.key and m.key.upper() == (ref.pmo_id or "").upper()):
                return m
        raise LookupError(ref.pmo_id)

    async def get(self, ref):
        self.get_calls.append(ref.pmo_id)
        return self._find(ref)

    async def set_status(self, ref, status):
        m = self._find(ref)
        self.statuses.append(status)
        m.status = status

    async def swap_labels(self, ref, remove, add):
        m = self._find(ref)
        self.swaps.append((set(remove), set(add)))
        m.labels = (m.labels - set(remove)) | set(add)

    async def get_activity(self, ref, full=False):
        self.activity_calls.append((ref.pmo_id, full))
        if ref.pmo_id in self.fail_ids:
            raise RuntimeError(f"activity unreadable: {ref.pmo_id}")
        act = self.activities.get(ref.pmo_id)
        if act is None:
            raise RuntimeError(f"no activity for {ref.pmo_id}")
        return act


def _act(mission: Mission, body: str, *, attachment: str | None = None) -> Activity:
    atts = []
    if attachment:
        atts = [AttachmentRef(url=f"https://uploads.example/{attachment}",
                              name=attachment)]
    entries = [ActivityEntry(ts=NOW, author="alice", kind="comment",
                             body=body, entry_id=f"e-{mission.pmo_id}",
                             attachments=atts)]
    return Activity(mission=mission, entries=entries)


def test_grandchild_payload_includes_root_and_parent_upstream(tmp_path):
    """The failing fleet case: grandchild sees ROOT content, not only parent."""
    root = _m("r", "ROOT-1")
    child = _m("c", "CHILD-1", parent="r", depth=1)
    grand = _m("g", "GRAND-1", parent="c", depth=2)
    activities = {
        "r": _act(root, "root context REVIEW_LEDGER", attachment="REVIEW_LEDGER.md"),
        "c": _act(child, "child plan note", attachment="PLAN.md"),
        "g": _act(grand, "grandchild feed"),
    }
    pmo = MultiActivityPMO([root, child, grand], activities)

    async def download_asset(url):
        name = url.rsplit("/", 1)[-1]
        return f"bytes-of-{name}".encode()

    pmo.download_asset = download_asset
    mgr = make_mgr(tmp_path, pmo)
    # Snapshot for ancestry walk (dispatch will list_all or pass missions).
    mgr._upstream_missions = [root, child, grand]
    payload = run_coro(mgr.activity_payload("g"))

    by_name = {a["filename"]: base64.b64decode(a["content_b64"])
               for a in payload["attachments"]}
    assert b"root context REVIEW_LEDGER" in by_name["upstream/ROOT-1/ACTIVITY.md"]
    assert by_name["upstream/ROOT-1/REVIEW_LEDGER.md"] == b"bytes-of-REVIEW_LEDGER.md"
    assert b"child plan note" in by_name["upstream/CHILD-1/ACTIVITY.md"]
    assert by_name["upstream/CHILD-1/PLAN.md"] == b"bytes-of-PLAN.md"
    assert "upstream/ROOT-1/" in payload["activity_md"] or \
        "upstream/" in payload["activity_md"]


def test_strict_unreadable_ancestor_reports_gap(tmp_path):
    """Unreadable ancestor → upstream_gaps entry (dispatch gates on this)."""
    root = _m("r", "ROOT-1")
    child = _m("c", "CHILD-1", parent="r", depth=1)
    activities = {
        "c": _act(child, "child only"),
    }
    pmo = MultiActivityPMO([root, child], activities)
    pmo.fail_ids.add("r")
    mgr = make_mgr(tmp_path, pmo)
    mgr._upstream_missions = [root, child]
    payload = run_coro(mgr.activity_payload("c"))
    gaps = payload.get("upstream_gaps") or []
    assert any(g.get("key") == "ROOT-1" for g in gaps)
    assert "ROOT-1" in payload["activity_md"]
    assert "unavailable" in payload["activity_md"].lower() or \
        "gap" in payload["activity_md"].lower()


def test_over_cap_truncates_oldest_ancestor_first(tmp_path):
    """Total upstream budget = attachment cap; oldest (root) drops first."""
    root = _m("r", "ROOT-1")
    child = _m("c", "CHILD-1", parent="r", depth=1)
    grand = _m("g", "GRAND-1", parent="c", depth=2)
    # Bodies sized so one ancestor mirror fits a ~900-byte budget and two
    # do not (each nested MISSION+ACTIVITY mirror is a few hundred bytes).
    activities = {
        "r": _act(root, "R" * 200),
        "c": _act(child, "C" * 200),
        "g": _act(grand, "grandchild"),
    }
    pmo = MultiActivityPMO([root, child, grand], activities)
    mgr = make_mgr(tmp_path, pmo)
    mgr._attachment_cap = lambda: 900  # nearer parent fits; root does not
    mgr._upstream_missions = [root, child, grand]
    payload = run_coro(mgr.activity_payload("g"))
    names = [a["filename"] for a in payload["attachments"]]
    assert any(n.startswith("upstream/CHILD-1/") for n in names)
    assert not any(n.startswith("upstream/ROOT-1/") for n in names)
    truncated = payload.get("upstream_truncated") or []
    assert "ROOT-1" in truncated
    assert "oldest" in payload["activity_md"].lower()
    assert "ROOT-1" in payload["activity_md"]


def test_bundled_skills_never_include_review_ledger():
    """Hotfix skill must not re-enter the bundled seed (skills philosophy)."""
    from pathlib import Path
    from devcake.domain import skills as skills_mod
    builtin = Path(skills_mod.BUILTIN_DIR)
    assert builtin.is_dir()
    names = {p.name for p in builtin.iterdir() if p.is_dir()}
    assert "review-ledger" not in names
    for p in builtin.rglob("*"):
        assert "review-ledger" not in p.as_posix()


def _dispatch_with_ancestry(tmp_path, *, strict: bool, fail_root: bool = True):
    """Dispatch a child whose parent activity may be unreadable."""
    from devcake.config import AppConfig, Assignment, DevType, PMOInstance
    from devcake.domain.model import MissionType
    from fakes import FakeInternalForge, make_mission_manager
    from test_prompt_templates import _ForgeWithDescriptor

    root = _m("r", "ROOT-1")
    child = _m("c", "CHILD-1", parent="r", depth=1)
    child.labels |= {"DEVCAKE-EXECUTE"}
    child.repo = "main"
    activities = {
        "r": _act(root, "root body"),
        "c": _act(child, "child body"),
    }
    pmo = MultiActivityPMO([root, child], activities)
    if fail_root:
        pmo.fail_ids.add("r")
    cfg = AppConfig(
        context_sourcing_strict=strict,
        assignments={mt: Assignment(dev_type="senior-dev")
                     for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")})
    mgr = make_mission_manager(
        tmp_path, pmo=pmo, forge=_ForgeWithDescriptor(), config=cfg,
        dev_types={"senior-dev": DevType(name="senior-dev",
                                         harness_template="claude-code")},
        noop_audit=True)
    mgr.internal_forge = FakeInternalForge()
    mgr.instance = PMOInstance(name="linear", team_key="DEV", repos=["main"])
    mgr._upstream_missions = [root, child]
    launched = []

    async def launch(run, image):
        launched.append(run)
    mgr.runs.bootstrap = type("B", (), {"launch": staticmethod(launch)})()
    run = run_coro(mgr.dispatch(child, MissionType.EXECUTE,
                                mgr.dev_types["senior-dev"]))
    return mgr, child, run, launched, mgr.internal_forge


def test_strict_unreadable_ancestor_gates_dispatch(tmp_path):
    """Strict on: unreadable ancestor → fail-closed, no attempt burned."""
    mgr, child, run, launched, forge = _dispatch_with_ancestry(
        tmp_path, strict=True, fail_root=True)
    assert run is None
    assert launched == []
    assert forge.pushes == []
    reason = mgr.blocked_reasons[child.pmo_id]
    assert "upstream activity unavailable" in reason
    assert "ROOT-1" in reason


def test_nonstrict_unreadable_ancestor_dispatches_with_gap(tmp_path):
    """Strict off: dispatch proceeds; payload/snapshot discloses the gap."""
    mgr, child, run, launched, forge = _dispatch_with_ancestry(
        tmp_path, strict=False, fail_root=True)
    assert run is not None and launched
    assert forge.pushes, "activity snapshot should still push"
    _repo, files, _msg = forge.pushes[0]
    by_path = {f["path"]: base64.b64decode(f["content_b64"]).decode()
               for f in files}
    assert "ROOT-1" in by_path["ACTIVITY.md"]
    assert "unavailable" in by_path["ACTIVITY.md"].lower() or \
        "UPSTREAM GAP" in by_path["ACTIVITY.md"]
