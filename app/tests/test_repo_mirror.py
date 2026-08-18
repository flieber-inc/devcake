"""RepoCache (ADR-0024) — pure decisions with a fake git runner.

The real-git contract (init/fetch/prune/HEAD/depth over file://) lives in
test_repo_mirror_git.py; here every subprocess is a recorded fake, so these
pin the DECISIONS: command sequences, coalescing, freshness, classification,
breaker latching, and the never-raises gate contract.
"""
import asyncio
from pathlib import Path

import pytest

from devcake.adapters.git import GitResult
from devcake.config import AppConfig, PMOInstance, RepoInstance
from devcake.domain.repo_mirror import (NullRepoCache, RepoCache,
                                        sync_error_class)

# House loop convention: the legacy suites drive coroutines via
# asyncio.get_event_loop().run_until_complete — asyncio.run() would CLOSE and
# UNSET the loop and poison the policy for every later test file. One shared
# loop, left set and open, keeps the whole suite compatible.
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run_coro(c):
    return _LOOP.run_until_complete(c)


class FakeForge:
    class descriptor:
        clone_user = "oauth2"


class FakeForges:
    """Just enough ForgeRuntime: instances/internal/latch."""

    def __init__(self, repos, internal=()):
        self.instances = {r.name: r for r in repos}
        self._forge = FakeForge()
        self.internal = set(internal)
        self.breakers: dict[str, str] = {}

    def instance(self, name):
        return self.instances.get(name)

    def get(self, name):
        return self._forge if name in self.instances else None

    def latch(self, name, reason):
        self.breakers[name] = reason


class Repo(RepoInstance):
    """RepoInstance whose tokens are plain fields, not secret read-throughs."""
    fake_token: str = ""
    fake_token_ro: str = ""

    @property
    def token(self):  # type: ignore[override]
        return self.fake_token

    @property
    def token_ro(self):  # type: ignore[override]
        return self.fake_token_ro


def make_cache(tmp_path, repos, *, internal=(), lfs=False, max_age=0,
               script=None):
    """(cache, calls). `script(args) -> GitResult` optional; default all-OK
    with get-url answering the expected URL."""
    cfg = AppConfig(pmos=[], repos=list(repos))
    cfg.repo_mirror.lfs = lfs
    cfg.repo_mirror.sync_max_age_seconds = max_age
    forges = FakeForges(repos, internal=internal)
    calls: list[list[str]] = []

    async def git(args, *, cwd=None, env=None, timeout=None):
        calls.append(list(args))
        if script is not None:
            r = script(args)
            if r is not None:
                return r
        if args[:1] == ["init"]:
            # the fake must materialize the dir — sync_one branches on it
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
        if "get-url" in args:
            name = Path(args[1]).name.removesuffix(".git")
            inst = forges.instance(name)
            user = FakeForge.descriptor.clone_user
            url = inst.url.replace("https://", f"https://{user}@")
            return GitResult(0, url + "\n", "")
        return GitResult(0, "", "")

    cache = RepoCache(cfg, forges, root=tmp_path, git=git)
    return cache, calls, forges


R1 = Repo(name="alpha", url="https://gitlab.com/o/alpha.git",
          fake_token_ro="ro-a", fake_token="rw-a")
R2 = Repo(name="beta", url="https://gitlab.com/o/beta.git", fake_token="rw-b")
PUB = Repo(name="pub", url="https://gitlab.com/o/pub.git")


# ── sync_one command sequences ───────────────────────────────────────────────

def test_first_sync_inits_fetches_heads_tags_and_sets_head(tmp_path):
    cache, calls, _ = make_cache(tmp_path, [R1])
    st = run_coro(cache.sync_one("alpha"))
    assert st.ok and st.synced_at is not None
    flat = ["\x1f".join(c) for c in calls]
    assert any(c.startswith("init\x1f--bare") for c in flat)
    assert any("remote\x1fadd\x1forigin\x1fhttps://oauth2@gitlab.com/o/alpha.git"
               in c for c in flat)
    assert any("config\x1fgc.auto\x1f0" in c for c in flat)
    fetch = next(c for c in calls if "fetch" in c)
    assert "--prune" in fetch
    assert "+refs/heads/*:refs/heads/*" in fetch
    assert "+refs/tags/*:refs/tags/*" in fetch
    # NEVER the firehose refspec (GitHub refs/pull/* would double disk)
    assert not any("+refs/*:refs/*" in " ".join(c) for c in calls)
    head = next(c for c in calls if "symbolic-ref" in c)
    assert head[-1] == "refs/heads/main"
    assert not any("lfs" in c for c in calls)          # lfs off by default


def test_existing_mirror_skips_init_and_fetches(tmp_path):
    cache, calls, _ = make_cache(tmp_path, [R1])
    cache.mirror_path("alpha").mkdir(parents=True)
    st = run_coro(cache.sync_one("alpha"))
    assert st.ok
    assert not any(c[:1] == ["init"] for c in calls)
    assert any("get-url" in c for c in calls)
    assert any("fetch" in c for c in calls)


def test_url_change_reinitializes(tmp_path):
    def script(args):
        if "get-url" in args:
            return GitResult(0, "https://oauth2@old.example/x.git\n", "")
        if args[:1] == ["init"]:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return None
    cache, calls, _ = make_cache(tmp_path, [R1], script=script)
    cache.mirror_path("alpha").mkdir(parents=True)
    st = run_coro(cache.sync_one("alpha"))
    assert st.ok
    assert any(c[:1] == ["init"] for c in calls)       # rebuilt, not fetched-into
    assert not cache.mirror_path("alpha").with_suffix(".git.stale").exists()


def test_lfs_flag_appends_default_branch_scoped_fetch(tmp_path):
    cache, calls, _ = make_cache(tmp_path, [R1], lfs=True)
    assert run_coro(cache.sync_one("alpha")).ok
    lfs = next(c for c in calls if "lfs" in c)
    assert lfs[-1] == "main" and "fetch" in lfs        # default-branch scope v1


def test_token_env_prefers_read_only_and_public_repo_goes_bare(tmp_path):
    envs = []

    async def git(args, *, cwd=None, env=None, timeout=None):
        envs.append(env or {})
        if args[:1] == ["init"]:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return GitResult(0, "", "")

    cfg = AppConfig(pmos=[], repos=[R1, PUB])
    cache = RepoCache(cfg, FakeForges([R1, PUB]), root=tmp_path, git=git)
    run_coro(cache.sync_one("alpha"))
    assert any(e.get("DEVCAKE_MIRROR_TOKEN") == "ro-a" for e in envs)
    envs.clear()
    run_coro(cache.sync_one("pub"))
    assert all("DEVCAKE_MIRROR_TOKEN" not in e for e in envs)


# ── classification + breaker ─────────────────────────────────────────────────

@pytest.mark.parametrize("stderr", [
    "fatal: Authentication failed for 'https://…'",
    "could not read Username for 'https://…'",
    "remote: HTTP Basic: Access denied … returned error: 403",
    "invalid credentials",
])
def test_auth_wording_latches_the_repo_breaker(tmp_path, stderr):
    def script(args):
        return GitResult(128, "", stderr) if "fetch" in args else None
    cache, _, forges = make_cache(tmp_path, [R1], script=script)
    st = run_coro(cache.sync_one("alpha"))
    assert not st.ok and st.auth
    assert "alpha" in forges.breakers
    assert forges.breakers["alpha"].startswith("mirror sync:")


@pytest.mark.parametrize("stderr,rc", [
    ("fatal: repository 'https://…' not found", 128),   # 404 ≠ credentials
    ("Could not resolve host: gitlab.com", 128),
    ("git fetch timed out after 900s", 124),
    ("error: RPC failed; HTTP 500", 128),
])
def test_non_auth_failures_gate_without_latching(tmp_path, stderr, rc):
    def script(args):
        return GitResult(rc, "", stderr) if "fetch" in args else None
    cache, _, forges = make_cache(tmp_path, [R1], script=script)
    st = run_coro(cache.sync_one("alpha"))
    assert not st.ok and not st.auth
    assert forges.breakers == {}


def test_sync_error_class_table():
    assert sync_error_class("Authentication failed") == "auth"
    assert sync_error_class("repository not found") == "transient"
    assert sync_error_class("anything else entirely") == "transient"


# ── ensure_fresh: gate semantics, coalescing, freshness ──────────────────────

def test_ensure_fresh_reports_failures_and_never_raises(tmp_path):
    async def git(args, *, cwd=None, env=None, timeout=None):
        raise RuntimeError("boom")
    cfg = AppConfig(pmos=[], repos=[R1])
    cache = RepoCache(cfg, FakeForges([R1]), root=tmp_path, git=git)
    ok, why = run_coro(cache.ensure_fresh(["alpha"]))
    assert not ok and "alpha" in why


def test_concurrent_waiters_coalesce_onto_one_fetch(tmp_path):
    fetches = 0

    def script(args):
        nonlocal fetches
        if "fetch" in args:
            fetches += 1
        return None

    cache, _, _ = make_cache(tmp_path, [R1], script=script)

    async def race():
        return await asyncio.gather(cache.ensure_fresh(["alpha"]),
                                    cache.ensure_fresh(["alpha"]))

    (ok1, _), (ok2, _) = run_coro(race())
    assert ok1 and ok2
    assert fetches == 1          # the second waiter accepted the peer's sync


def test_max_age_window_short_circuits(tmp_path):
    fetches = 0

    def script(args):
        nonlocal fetches
        if "fetch" in args:
            fetches += 1
        return None

    cache, _, _ = make_cache(tmp_path, [R1], max_age=3600, script=script)
    assert run_coro(cache.ensure_fresh(["alpha"]))[0]
    assert run_coro(cache.ensure_fresh(["alpha"]))[0]
    assert fetches == 1


def test_max_age_zero_resyncs_on_every_call(tmp_path):
    fetches = 0

    def script(args):
        nonlocal fetches
        if "fetch" in args:
            fetches += 1
        return None

    cache, _, _ = make_cache(tmp_path, [R1], max_age=0, script=script)
    run_coro(cache.ensure_fresh(["alpha"]))
    run_coro(cache.ensure_fresh(["alpha"]))
    assert fetches == 2


def test_volume_error_gates_everything_with_the_fix(tmp_path):
    cache, _, _ = make_cache(tmp_path, [R1])
    cache.volume_error = "EACCES — the mirrors volume is not writable"
    ok, why = run_coro(cache.ensure_fresh(["alpha"]))
    assert not ok and "mirror volume" in why["alpha"]
    assert run_coro(cache.ensure_fresh([])) == (True, {})


# ── needed_for (the gate's repo set) ─────────────────────────────────────────

def _inst(repos=(), refs=()):
    return PMOInstance(name="linear", system="linear", team_key="T",
                       repos=list(repos), reference_repos=list(refs))


def test_needed_for_table(tmp_path):
    cache, _, _ = make_cache(tmp_path, [R1, R2, PUB], internal=["int-1"])
    inst = _inst(repos=["alpha", "beta"], refs=["pub"])
    blockers = [{"repo_ref": "beta"}, {"repo_ref": "int-1"},
                {"repo_ref": "gone"}]
    onboard = cache.needed_for(work_repo="alpha", mission_type="ONBOARD",
                               instance=inst, blocker_entries=blockers)
    assert onboard == ["alpha", "beta", "pub"]     # dedup; internal + vanished dropped
    execute = cache.needed_for(work_repo="beta", mission_type="EXECUTE",
                               instance=inst, blocker_entries=[])
    assert execute == ["beta", "pub"]              # routing set is ONBOARD-only
    steward = cache.needed_for(work_repo="alpha", mission_type="STEWARD",
                              instance=inst, blocker_entries=[])
    assert steward == ["alpha"]
    internal_work = cache.needed_for(work_repo="int-1", mission_type="EXECUTE",
                                     instance=inst, blocker_entries=[])
    assert internal_work == ["pub"]                # internal work repo → extras only


def test_eligible_excludes_internal_and_unknown(tmp_path):
    cache, _, _ = make_cache(tmp_path, [R1], internal=["int-1"])
    assert cache.eligible("alpha")
    assert not cache.eligible("int-1")
    assert not cache.eligible("never-configured")


def test_has_last_good_requires_branch_content_not_bare_dir(tmp_path):
    """Open-mode stale_cache (ADR-0035 / PLAN_MEMORY §3.5) means a prior
    successful sync left branch content — not that a bare dir exists.
    Bare `git init --bare` alone (fetch never succeeded) must be False so
    classify_context_failures omits rather than proceeding on empty heads."""
    cache, _, _ = make_cache(tmp_path, [R1])
    assert not cache.has_last_good("alpha")
    # bare dir only — the pre-fetch state after a failed first sync
    p = cache.mirror_path("alpha")
    p.mkdir(parents=True)
    (p / "refs" / "heads").mkdir(parents=True)
    (p / "HEAD").write_text("ref: refs/heads/main\n")
    assert p.is_dir() and not cache.has_last_good("alpha")
    # after a successful sync the ledger + heads are populated
    assert run_coro(cache.sync_one("alpha")).ok
    # materialize a branch ref the way a real fetch would (fake git does not)
    (p / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    assert cache.has_last_good("alpha")
    # failed re-sync keeps last-good (synced_at preserved; heads remain)
    def fail_fetch(args):
        if "fetch" in args:
            return GitResult(128, "", "fatal: unable to access")
        return None
    cache2, _, _ = make_cache(tmp_path / "m2", [R1], script=fail_fetch)
    good = cache2.mirror_path("alpha")
    good.mkdir(parents=True)
    (good / "refs" / "heads").mkdir(parents=True)
    (good / "refs" / "heads" / "main").write_text("b" * 40 + "\n")
    # seed a prior success in the ledger without going through git
    from datetime import datetime, timezone
    from devcake.domain.repo_mirror import MirrorStatus
    cache2.ledger["alpha"] = MirrorStatus(
        ok=True, synced_at=datetime.now(timezone.utc),
        attempted_at=datetime.now(timezone.utc))
    cache2._synced_mono["alpha"] = cache2._monotonic()
    assert cache2.has_last_good("alpha")
    st = run_coro(cache2.sync_one("alpha"))
    assert not st.ok and st.synced_at is not None
    assert cache2.has_last_good("alpha")
    assert NullRepoCache().has_last_good("anything") is False


# ── hygiene ──────────────────────────────────────────────────────────────────

def test_delete_mirror_renames_aside_then_removes(tmp_path):
    cache, _, _ = make_cache(tmp_path, [R1])
    p = cache.mirror_path("alpha")
    p.mkdir(parents=True)
    (p / "config").write_text("x")
    cache.delete_mirror("alpha")
    assert not p.exists()
    assert not list(tmp_path.glob("alpha.git.stale-*"))   # aside dir removed too
    cache.delete_mirror("alpha")                          # missing → no-op


def test_verify_writable_sets_and_clears_volume_error(tmp_path):
    cache, _, _ = make_cache(tmp_path / "mirrors", [R1])
    cache.verify_writable()
    assert cache.volume_error is None
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    cache.root = ro
    cache.verify_writable()
    assert cache.volume_error and "docker volume rm devcake_mirrors" in cache.volume_error
    ro.chmod(0o755)


# ── the dispatch gate (full dispatch through the real chokepoint) ────────────

class RefusingCache(NullRepoCache):
    """Fresh never: the gate must defer without a container or a push."""

    def needed_for(self, *, work_repo, **_kw):
        return [work_repo]

    async def ensure_fresh(self, names):
        return False, {n: "fetch: fatal: unable to access (transient)"
                       for n in names}


class GrantingCache(NullRepoCache):
    """Everything eligible + fresh: dispatch proceeds with a mirror path."""

    def eligible(self, name):
        return True


def test_dispatch_defers_on_stale_mirror_no_push_no_launch(tmp_path):
    from test_activity_repos import _dispatch_setup
    from fakes import FakeInternalForge
    from devcake.domain.model import MissionType
    forge = FakeInternalForge()
    mgr, fake, m, launched = _dispatch_setup(tmp_path, forge)
    mgr.repo_cache = RefusingCache()
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE,
                                mgr.dev_types["senior-dev"]))
    assert run is None
    assert launched == []                       # no container
    assert forge.pushes == []                   # gate sits BEFORE the activity
    #                                             push — no snapshot per gated cycle
    reason = mgr.blocked_reasons[m.pmo_id]
    assert "repository mirror not fresh" in reason and "main" in reason


def test_dispatch_proceeds_with_mirror_path_in_spec_env(tmp_path):
    from test_activity_repos import _dispatch_setup
    from fakes import FakeInternalForge
    from devcake.domain.model import MissionType
    mgr, fake, m, launched = _dispatch_setup(tmp_path, FakeInternalForge())
    mgr.repo_cache = GrantingCache()
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE,
                                mgr.dev_types["senior-dev"]))
    assert run is not None and launched
    assert run.spec_env["DEVCAKE_MIRROR_PATH"] == "/mirrors/main.git"
    assert run.spec_env["DEVCAKE_LFS"] == ""    # default off; "1"/"" convention


def test_dispatch_direct_clone_for_ineligible_repo(tmp_path):
    from test_activity_repos import _dispatch_setup
    from fakes import FakeInternalForge
    from devcake.domain.model import MissionType
    mgr, fake, m, launched = _dispatch_setup(tmp_path, FakeInternalForge())
    # NullRepoCache: nothing eligible ⇒ mirror path empty ⇒ direct clone
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE,
                                mgr.dev_types["senior-dev"]))
    assert run is not None
    assert run.spec_env["DEVCAKE_MIRROR_PATH"] == ""


# ── the runspec serves EXACTLY what the gate proved (2026-08 eval F12) ───────

class TmpRootCache(NullRepoCache):
    """Real mirror paths under tmp, so a mirror's presence is a per-test
    fact — the belt check in dispatch._mirrored is part of the contract."""

    def __init__(self, root, eligible=()):
        super().__init__()
        self._root = Path(root)
        self._eligible = set(eligible)

    def mirror_path(self, name):
        return self._root / f"{name}.git"

    def eligible(self, name):
        return name in self._eligible


def _extras_rig(tmp_path, monkeypatch):
    """A manager whose forges hold a work repo (alpha) + reference repo
    (beta, RO token stored) and whose repo_cache roots under tmp."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.domain.forge_runtime import ForgeRuntime
    from devcake.adapters.registry import make_forge
    from devcake import secrets as s
    from fakes import make_mission_manager
    s.write_connection_secret("repo", "beta", "token_ro", "beta-read")
    rt = ForgeRuntime()
    rt.rebuild([RepoInstance(name="alpha", url="https://github.com/o/a"),
                RepoInstance(name="beta", url="https://github.com/o/b")],
               make_forge)
    mgr = make_mission_manager(tmp_path)
    mgr.forges = rt
    mgr.instance = PMOInstance(name="linear", team_key="DEV", repos=["alpha"],
                               reference_repos=["beta"])
    mgr.repo_cache = TmpRootCache(tmp_path / "mirrors",
                                  eligible={"alpha", "beta"})
    return mgr


def _execute_run(**kw):
    from devcake.domain.run import Run
    base = dict(run_id="LINEAR-T-B-1-EXECUTE-AAAAAA", mission_key="T-B",
                mission_type="EXECUTE", dev_type="judgment", seq=1,
                repo_ref="alpha", pmo_ref="linear")
    base.update(kw)
    return Run(**base)


def test_runspec_serves_the_dispatch_snapshot(tmp_path, monkeypatch):
    """A snapshot member with a live mirror rides the mirror: mirror_path,
    no token in transit (the ADR-0024 rationale)."""
    from devcake.domain.orchestrator import dispatch
    mgr = _extras_rig(tmp_path, monkeypatch)
    mgr.repo_cache.mirror_path("beta").mkdir(parents=True)
    run = _execute_run(mirror_repos=["alpha", "beta"])
    item = next(x for x in dispatch._extra_repos_for(mgr, run)
                if x["name"] == "beta")
    assert item["mirror_path"].endswith("beta.git")
    assert "token" not in item


def test_repo_added_after_dispatch_never_gets_a_mirror_path(tmp_path,
                                                            monkeypatch):
    """THE race regression (2026-08 eval F12): a repo added to the instance
    between dispatch and runspec.get is NOT in the gate's snapshot, so it
    must never be served a mirror_path the gate never proved — even when a
    mirror directory happens to exist. It rides its token instead."""
    from devcake.domain.orchestrator import dispatch
    mgr = _extras_rig(tmp_path, monkeypatch)
    mgr.repo_cache.mirror_path("beta").mkdir(parents=True)   # exists, unproven
    run = _execute_run(mirror_repos=["alpha"])   # beta joined after dispatch
    item = next(x for x in dispatch._extra_repos_for(mgr, run)
                if x["name"] == "beta")
    assert "mirror_path" not in item
    assert item["token"] == "beta-read"


def test_vanished_mirror_falls_back_to_token_clone(tmp_path, monkeypatch):
    """Belt: a snapshot member whose mirror was wiped mid-flight must not
    hand the Dev a dead file:// URL — the token path is the graceful
    degradation (extra-clone failures are non-fatal in the entrypoint, but
    real context beats a confusing failure note)."""
    from devcake.domain.orchestrator import dispatch
    mgr = _extras_rig(tmp_path, monkeypatch)
    run = _execute_run(mirror_repos=["alpha", "beta"])   # no dir on disk
    item = next(x for x in dispatch._extra_repos_for(mgr, run)
                if x["name"] == "beta")
    assert "mirror_path" not in item
    assert item["token"] == "beta-read"


def test_legacy_run_without_snapshot_keeps_live_derivation(tmp_path,
                                                           monkeypatch):
    """A pre-field record (empty mirror_repos) keeps the pre-fix behavior:
    live eligibility decides, so an upgrade never strands in-flight runs."""
    from devcake.domain.orchestrator import dispatch
    mgr = _extras_rig(tmp_path, monkeypatch)
    mgr.repo_cache.mirror_path("beta").mkdir(parents=True)
    run = _execute_run()                                  # mirror_repos == []
    item = next(x for x in dispatch._extra_repos_for(mgr, run)
                if x["name"] == "beta")
    assert item["mirror_path"].endswith("beta.git")


def test_dispatch_snapshots_the_gate_set_on_the_run(tmp_path):
    """The producing side of the parity contract: dispatch() records
    needed_for's exact output on run.mirror_repos."""
    from test_activity_repos import _dispatch_setup
    from fakes import FakeInternalForge
    from devcake.domain.model import MissionType

    class SnapshottingCache(GrantingCache):
        def needed_for(self, *, work_repo, **_kw):
            return [work_repo]

    mgr, fake, m, launched = _dispatch_setup(tmp_path, FakeInternalForge())
    mgr.repo_cache = SnapshottingCache()
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE,
                                mgr.dev_types["senior-dev"]))
    assert run is not None
    assert run.mirror_repos == ["main"]


# ── the Null stand-in keeps the rest of the suite honest ─────────────────────

def test_null_repo_cache_is_always_fresh_and_never_mirrors():
    null = NullRepoCache()
    assert run_coro(null.ensure_fresh(["anything"])) == (True, {})
    assert not null.eligible("anything")
    assert null.needed_for(work_repo="x", mission_type="EXECUTE",
                           instance=None, blocker_entries=[]) == []
    assert null.health_map() == {} and null.volume_error is None


# ── skill-source cards join the gate's needed-set (ADR-0016 addendum) ────────

class RecordingGrantingCache(GrantingCache):
    def __init__(self):
        super().__init__()
        self.asked: list[list[str]] = []
        self.heads: dict[str, str] = {"skillrepo": "cafe1234"}

    async def ensure_fresh(self, names):
        self.asked.append(sorted(names))
        return True, {}

    async def tree_head(self, name):
        return self.heads.get(name)


def test_dispatch_gate_unions_skill_cards_and_stamps_heads(tmp_path):
    from test_activity_repos import _dispatch_setup
    from fakes import FakeInternalForge
    from devcake.domain.model import MissionType
    mgr, fake, m, launched = _dispatch_setup(tmp_path, FakeInternalForge())
    cache = RecordingGrantingCache()
    mgr.repo_cache = cache
    dt = mgr.dev_types["senior-dev"]
    dt.skills = ["skillrepo/tdd"]
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE, dt))
    assert run is not None
    assert any("skillrepo" in asked for asked in cache.asked)
    assert "skillrepo" in run.mirror_repos          # truthful gate snapshot
    assert run.skill_repo_heads == {"skillrepo": "cafe1234"}


def test_dispatch_defers_when_a_skill_card_is_stale(tmp_path):
    from test_activity_repos import _dispatch_setup
    from fakes import FakeInternalForge
    from devcake.domain.model import MissionType

    class SkillStale(GrantingCache):
        async def ensure_fresh(self, names):
            bad = {n: "unknown card" for n in names if n == "ghost"}
            return (not bad), bad

    forge = FakeInternalForge()
    mgr, fake, m, launched = _dispatch_setup(tmp_path, forge)
    mgr.repo_cache = SkillStale()
    dt = mgr.dev_types["senior-dev"]
    dt.skills = ["ghost/tdd"]
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE, dt))
    assert run is None and launched == [] and forge.pushes == []
    assert "ghost" in mgr.blocked_reasons[m.pmo_id]


def test_dispatch_without_external_skills_asks_for_no_cards(tmp_path):
    from test_activity_repos import _dispatch_setup
    from fakes import FakeInternalForge
    from devcake.domain.model import MissionType
    mgr, fake, m, launched = _dispatch_setup(tmp_path, FakeInternalForge())
    cache = RecordingGrantingCache()
    mgr.repo_cache = cache
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE,
                                mgr.dev_types["senior-dev"]))
    assert run is not None
    assert all("skillrepo" not in asked for asked in cache.asked)
    assert run.skill_repo_heads == {}
