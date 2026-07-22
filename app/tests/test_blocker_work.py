"""Done blockers' work repos as RO extra_repos (plan slice B)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from devcake.config import AppConfig, PMOInstance, RepoInstance
from devcake.domain.model import Mission
from devcake.domain.orchestrator import dispatch
from devcake.domain.run import Run
from fakes import make_mission_manager

NOW = datetime.now(timezone.utc)


def run_coro(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


def _mission(pmo_id, key, status="backlog", blocked_by=()):
    return Mission(
        pmo_id=pmo_id, pmo_kind="issue", instance="linear", key=key,
        title=key, status=status, labels={"DEVCAKE"}, updated_at=NOW,
        blocked_by=list(blocked_by))


def _run(mid, key, repo_ref, *, created=None):
    r = Run(run_id=f"LINEAR-{key}-1-EXECUTE-AAAAAA", mission_key=key,
            mission_type="EXECUTE", dev_type="d", seq=1, repo_ref=repo_ref,
            mission_pmo_id=mid, pmo_ref="linear")
    if created is not None:
        r.created_at = created
    return r


class BlockerPMO:
    def __init__(self, missions: dict[str, Mission]):
        self.missions = missions

    async def get(self, ref):
        m = self.missions.get(ref.pmo_id)
        if m is None:
            raise RuntimeError(f"missing {ref.pmo_id}")
        return m


def test_resolve_blocker_work_done_different_repo(tmp_path):
    a = _mission("a", "T-A", status="done")
    b = _mission("b", "T-B", blocked_by=["a"])
    runs = [_run("a", "T-A", "linear-t-a")]
    mgr = make_mission_manager(tmp_path, pmo=BlockerPMO({"a": a, "b": b}))
    entries, skips = run_coro(
        dispatch.resolve_blocker_work(mgr, b, "linear-t-b", runs))
    assert entries == [{"repo_ref": "linear-t-a", "mission_key": "T-A"}]
    assert skips == []


def test_resolve_blocker_work_ignores_mapper_and_foreign_instance(tmp_path):
    a = _mission("a", "T-A", status="done")
    b = _mission("b", "T-B", blocked_by=["a"])
    mapper = _run("a", "T-A", "evil-repo")
    mapper.mission_type = "MAPPER"
    foreign = _run("a", "T-A", "linear-t-a")
    foreign.pmo_ref = "other-instance"
    good = _run("a", "T-A", "linear-t-a")
    good.pmo_ref = "linear"
    mgr = make_mission_manager(tmp_path, pmo=BlockerPMO({"a": a, "b": b}))
    # manager instance name defaults to linear
    entries, _ = run_coro(
        dispatch.resolve_blocker_work(
            mgr, b, "primary", [mapper, foreign, good]))
    assert entries == [{"repo_ref": "linear-t-a", "mission_key": "T-A"}]


def test_resolve_blocker_work_same_repo_skipped(tmp_path):
    a = _mission("a", "T-A", status="done")
    b = _mission("b", "T-B", blocked_by=["a"])
    runs = [_run("a", "T-A", "shared")]
    mgr = make_mission_manager(tmp_path, pmo=BlockerPMO({"a": a, "b": b}))
    entries, skips = run_coro(
        dispatch.resolve_blocker_work(mgr, b, "shared", runs))
    assert entries == []
    assert skips == []


def test_resolve_blocker_work_canceled_skipped(tmp_path):
    a = _mission("a", "T-A", status="canceled")
    b = _mission("b", "T-B", blocked_by=["a"])
    runs = [_run("a", "T-A", "linear-t-a")]
    mgr = make_mission_manager(tmp_path, pmo=BlockerPMO({"a": a, "b": b}))
    entries, skips = run_coro(
        dispatch.resolve_blocker_work(mgr, b, "primary", runs))
    assert entries == []
    assert any("canceled" in s for s in skips)


def test_resolve_blocker_work_no_runs_skipped(tmp_path):
    a = _mission("a", "T-A", status="done")
    b = _mission("b", "T-B", blocked_by=["a"])
    mgr = make_mission_manager(tmp_path, pmo=BlockerPMO({"a": a, "b": b}))
    entries, skips = run_coro(
        dispatch.resolve_blocker_work(mgr, b, "primary", []))
    assert entries == []
    assert any("no prior work repo" in s for s in skips)


def test_resolve_blocker_work_cap(tmp_path):
    blockers = {_id: _mission(_id, f"T-{_id}", status="done")
                for _id in list("abcdefghi")}  # 9
    b = _mission("z", "T-Z", blocked_by=list("abcdefghi"))
    runs = [_run(_id, f"T-{_id}", f"repo-{_id}") for _id in "abcdefghi"]
    mgr = make_mission_manager(
        tmp_path, pmo=BlockerPMO({**blockers, "z": b}))
    entries, skips = run_coro(
        dispatch.resolve_blocker_work(mgr, b, "primary", runs, max_extras=8))
    assert len(entries) == 8
    assert any(s.startswith("cap 8:") for s in skips)


def test_blocker_repos_note_lists_paths(tmp_path):
    mgr = make_mission_manager(tmp_path)
    note = dispatch._blocker_repos_note(
        mgr,
        [{"repo_ref": "linear-t-a", "mission_key": "T-A"}],
        ["T-X: canceled — no work tree"])
    assert "Completed blocker work" in note
    assert "NEVER modify" in note
    assert "`T-A`" in note and "linear-t-a" in note
    assert "/workspace/repo/" in note
    assert "canceled" in note


def test_extra_repos_includes_blocker_work_internal(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.ports.internal_forge import MissionRepoCredentials

    class FakeInternal:
        def mission_credentials(self, name):
            if name != "linear-t-a":
                return None
            return MissionRepoCredentials(
                repo_name=name,
                clone_url="http://gitea:3000/devcake-internal/linear-t-a.git",
                username="svc-linear-t-a",
                token_write="write-secret",
                token_read="read-secret")

    mgr = make_mission_manager(tmp_path, internal_forge=FakeInternal())
    # need a primary repo registered so runspec doesn't fail elsewhere
    from devcake.domain.forge_runtime import ForgeRuntime
    from devcake.adapters.registry import make_forge
    from devcake import secrets as s
    s.write_connection_secret("repo", "alpha", "token", "alpha-tok")
    rt = ForgeRuntime()
    rt.rebuild([RepoInstance(name="alpha", url="https://github.com/o/a")],
               make_forge)
    mgr.forges = rt
    mgr.instance = PMOInstance(name="linear", team_key="DEV", repos=["alpha"])

    run = Run(run_id="LINEAR-T-B-1-EXECUTE-AAAAAA", mission_key="T-B",
              mission_type="EXECUTE", dev_type="judgment", seq=1,
              repo_ref="alpha", pmo_ref="linear",
              blocker_work=[{"repo_ref": "linear-t-a", "mission_key": "T-A"}])
    extras = dispatch._extra_repos_for(mgr, run)
    names = [x["name"] for x in extras]
    assert "linear-t-a" in names
    item = next(x for x in extras if x["name"] == "linear-t-a")
    assert item["token"] == "read-secret"
    assert "linear-t-a" in item["url"]

    # MAPPER never gets blocker extras
    mapper = Run(run_id="LINEAR-TEAM-1-MAPPER-AAAAAA", mission_key="TEAM",
                 mission_type="MAPPER", dev_type="judgment", seq=1,
                 repo_ref="alpha",
                 blocker_work=[{"repo_ref": "linear-t-a", "mission_key": "T-A"}])
    assert "linear-t-a" not in [
        x["name"] for x in dispatch._extra_repos_for(mgr, mapper)]


def test_prompt_includes_blocker_section():
    from devcake.prompts import execute_prompt
    m = _mission("b", "T-B")
    note = (
        "\n### Completed blocker work (read-only)\n"
        "…\n- `T-A` (`linear-t-a`) → /workspace/repo/linear-t-a/\n\n")
    out = execute_prompt("ID", m, "a", "open pr {branch}",
                         blocker_repos=note)
    assert "Completed blocker work" in out
    assert "linear-t-a" in out
