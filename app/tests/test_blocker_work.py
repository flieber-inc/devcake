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


class _ROInternal:
    """mission_credentials for ANY repo name — every blocker mountable
    (resolve_blocker_work checks mountability at dispatch time)."""

    def mission_credentials(self, name):
        from devcake.ports.internal_forge import MissionRepoCredentials
        return MissionRepoCredentials(
            repo_name=name,
            clone_url=f"http://gitea:3000/devcake-internal/{name}.git",
            username=f"svc-{name}",
            token_write="w", token_read="r")


def test_resolve_blocker_work_done_different_repo(tmp_path):
    a = _mission("a", "T-A", status="done")
    b = _mission("b", "T-B", blocked_by=["a"])
    runs = [_run("a", "T-A", "linear-t-a")]
    mgr = make_mission_manager(tmp_path, pmo=BlockerPMO({"a": a, "b": b}),
                               internal_forge=_ROInternal())
    entries, skips = run_coro(
        dispatch.resolve_blocker_work(mgr, b, "linear-t-b", runs))
    assert entries == [{"repo_ref": "linear-t-a", "mission_key": "T-A"}]
    assert skips == []


def test_resolve_blocker_work_ignores_mapper_and_unattributed_foreign(tmp_path):
    """Descendant of the pre-cross-instance guard: MAPPER runs never mount,
    and a run stamped by an instance the locator did NOT attribute the
    blocker to (here: unconfigured 'other-instance') stays invisible."""
    a = _mission("a", "T-A", status="done")
    b = _mission("b", "T-B", blocked_by=["a"])
    mapper = _run("a", "T-A", "evil-repo")
    mapper.mission_type = "MAPPER"
    foreign = _run("a", "T-A", "foreign-repo")
    foreign.pmo_ref = "other-instance"
    good = _run("a", "T-A", "linear-t-a")
    good.pmo_ref = "linear"
    mgr = make_mission_manager(tmp_path, pmo=BlockerPMO({"a": a, "b": b}),
                               internal_forge=_ROInternal())
    # manager instance name defaults to linear
    entries, _ = run_coro(
        dispatch.resolve_blocker_work(
            mgr, b, "primary", [mapper, foreign, good]))
    assert entries == [{"repo_ref": "linear-t-a", "mission_key": "T-A"}]


def _peer(name, system, missions):
    """Minimal peer manager for the locator (instance identity + pmo.get)."""
    class _PMO:
        async def get(self, ref):
            m = missions.get(ref.pmo_id)
            if m is None:
                raise RuntimeError(f"missing {ref.pmo_id}")
            return m
    return SimpleNamespace(
        instance=SimpleNamespace(name=name, system=system),
        instance_name=name, pmo=_PMO())


def test_resolve_blocker_work_accepts_attributed_peer_runs(tmp_path):
    """ADR-0009 amendment, the flagship case: an eng mission blocked by a
    done CS mission mounts the CS work tree — run history attributed to the
    cs instance via the locator, credentials via the SHARED internal forge
    (internal names are {instance}-{key})."""
    from devcake.domain.blocker_locator import BlockerLocator
    a = _mission("a", "CS-1", status="done")
    a.instance = "cs"
    b = _mission("b", "ENG-1", blocked_by=["a"])
    cs_run = _run("a", "CS-1", "cs-cs-1")
    cs_run.pmo_ref = "cs"
    mgr = make_mission_manager(tmp_path, pmo=BlockerPMO({"b": b}),
                               internal_forge=_ROInternal())
    cs = _peer("cs", "linear", {"a": a})
    locator = BlockerLocator({"linear": mgr, "cs": cs}, lambda bid: None)
    mgr.blocker_locator = locator
    entries, skips = run_coro(
        dispatch.resolve_blocker_work(mgr, b, "primary", [cs_run]))
    assert entries == [{"repo_ref": "cs-cs-1", "mission_key": "CS-1"}]
    assert skips == []


def test_resolve_blocker_work_colliding_gitea_id_never_mounts_peer(tmp_path):
    """Regression guard for the colliding-id hole: gitea_issues pmo_ids are
    per-repo issue NUMBERS, so a purely LOCAL blocker '3' must never index a
    peer instance's run for ITS OWN unrelated '3'."""
    from devcake.config import PMOInstance
    from devcake.domain.blocker_locator import BlockerLocator
    a = _mission("3", "#3", status="done")
    b = _mission("9", "#9", blocked_by=["3"])
    evil = _run("3", "#3", "evil-repo")
    evil.pmo_ref = "g2"                       # the OTHER gitea instance's #3
    mgr = make_mission_manager(
        tmp_path, pmo=BlockerPMO({"3": a, "9": b}),
        instance=PMOInstance(name="g1", system="gitea_issues",
                             team_key="o/r"),
        internal_forge=_ROInternal())
    g2 = _peer("g2", "gitea_issues", {"3": _mission("3", "#3", status="done")})
    mgr.blocker_locator = BlockerLocator(
        {"g1": mgr, "g2": g2}, lambda bid: None)
    entries, skips = run_coro(
        dispatch.resolve_blocker_work(mgr, b, "primary", [evil]))
    assert entries == []
    assert any("no prior work repo" in s for s in skips)


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
        tmp_path, pmo=BlockerPMO({**blockers, "z": b}),
        internal_forge=_ROInternal())
    entries, skips = run_coro(
        dispatch.resolve_blocker_work(mgr, b, "primary", runs, max_extras=8))
    assert len(entries) == 8
    assert any(s.startswith("cap 8:") for s in skips)


def test_resolve_blocker_work_unmountable_skipped(tmp_path):
    """A done blocker whose work repo has no read credential TODAY (cleared
    internal repo, removed instance) is a skip with a reason — the prompt
    must never list a mount runspec would silently omit."""
    a = _mission("a", "T-A", status="done")
    b = _mission("b", "T-B", blocked_by=["a"])
    runs = [_run("a", "T-A", "linear-t-a")]
    # no internal forge, no configured instance → nothing to mount with
    mgr = make_mission_manager(tmp_path, pmo=BlockerPMO({"a": a, "b": b}))
    entries, skips = run_coro(
        dispatch.resolve_blocker_work(mgr, b, "primary", runs))
    assert entries == []
    assert any("unavailable" in s for s in skips)


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


def test_extra_repos_includes_blocker_work_configured(tmp_path, monkeypatch):
    """The CONFIGURED-repo blocker branch: instance + forge must resolve and
    the read token is preferred over the write token."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.domain.forge_runtime import ForgeRuntime
    from devcake.adapters.registry import make_forge
    from devcake import secrets as s
    s.write_connection_secret("repo", "alpha", "token", "alpha-tok")
    s.write_connection_secret("repo", "beta", "token", "beta-write")
    s.write_connection_secret("repo", "beta", "token_ro", "beta-read")
    rt = ForgeRuntime()
    rt.rebuild([RepoInstance(name="alpha", url="https://github.com/o/a"),
                RepoInstance(name="beta", url="https://github.com/o/b")],
               make_forge)
    mgr = make_mission_manager(tmp_path)
    mgr.forges = rt
    mgr.instance = PMOInstance(name="linear", team_key="DEV", repos=["alpha"])

    run = Run(run_id="LINEAR-T-B-1-EXECUTE-AAAAAA", mission_key="T-B",
              mission_type="EXECUTE", dev_type="judgment", seq=1,
              repo_ref="alpha", pmo_ref="linear",
              blocker_work=[{"repo_ref": "beta", "mission_key": "T-A"}])
    extras = dispatch._extra_repos_for(mgr, run)
    item = next(x for x in extras if x["name"] == "beta")
    assert item["token"] == "beta-read"          # RO preferred over write
    assert item["url"] == "https://github.com/o/b"


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
