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
        self.breaker_fields: dict[str, str] = {}

    def instance(self, name):
        return self.instances.get(name)

    def get(self, name):
        return self._forge if name in self.instances else None

    def latch(self, name, reason, *, credential_field=None):
        self.breakers[name] = reason
        if credential_field is not None:
            self.breaker_fields[name] = credential_field


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
        if "symbolic-ref" in args and args[-2] == "HEAD":
            # like real git: HEAD is a file the resolver reads back
            mirror = Path(args[args.index("-C") + 1])
            mirror.mkdir(parents=True, exist_ok=True)
            (mirror / "HEAD").write_text(f"ref: {args[-1]}\n")
        if "get-url" in args:
            name = Path(args[1]).name.removesuffix(".git")
            inst = forges.instance(name)
            if inst is None:
                # a dedicated skill source: no forge adapter, no clone user
                src = next((x for x in (cfg.skill_sources or [])
                            if x.name == name), None)
                return GitResult(0, (src.url if src else "") + "\n", "")
            user = FakeForge.descriptor.clone_user
            url = RepoCache._origin_url(inst.url, user)
            return GitResult(0, url + "\n", "")
        if args[:2] == ["ls-remote", "--symref"]:
            # a blank card (the model default) resolves the remote's HEAD:
            # the fake remote's default is `main`
            return GitResult(0, "ref: refs/heads/main\tHEAD\n"
                                "0" * 40 + "\tHEAD\n", "")
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


def test_sync_one_injects_clone_user_on_http_url(tmp_path):
    """sync_one must embed clone_user for http:// the same way as https://
    (config._url_shape accepts http; remote_head already did)."""
    http = Repo(name="alpha", url="http://gitea:3000/o/alpha.git",
                fake_token_ro="ro-a", fake_token="rw-a")
    cache, calls, _ = make_cache(tmp_path, [http])
    st = run_coro(cache.sync_one("alpha"))
    assert st.ok
    flat = ["\x1f".join(c) for c in calls]
    assert any("remote\x1fadd\x1forigin\x1fhttp://oauth2@gitea:3000/o/alpha.git"
               in c for c in flat)


def test_sync_one_and_remote_head_share_origin_url_for_http_and_https(tmp_path):
    """One construction path — sync_one origin and remote_head ls-remote URL
    must agree for both schemes (ADR-0034 chokepoint)."""
    cases = [
        ("https://gitlab.com/o/alpha.git",
         "https://oauth2@gitlab.com/o/alpha.git"),
        ("http://gitea:3000/o/alpha.git",
         "http://oauth2@gitea:3000/o/alpha.git"),
    ]
    for card_url, expected in cases:
        repo = Repo(name="alpha", url=card_url,
                    fake_token_ro="ro-a", fake_token="rw-a")
        cache, calls, _ = make_cache(tmp_path / card_url.replace("://", "_")
                                     .replace("/", "_"), [repo])
        assert run_coro(cache.sync_one("alpha")).ok
        add = next(c for c in calls if "remote" in c and "add" in c)
        assert add[-1] == expected
        # remote_head uses the same helper; capture ls-remote URL
        calls.clear()
        run_coro(cache.remote_head("alpha"))
        ls = next(c for c in calls if "ls-remote" in c)
        assert ls[1] == expected


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
    # heads alone are not enough: last-good is "HEAD names a branch that is
    # there" — a mirror whose HEAD dangles must never serve as stale content
    assert not cache2.has_last_good("alpha")
    (good / "HEAD").write_text("ref: refs/heads/main\n")
    # seed a prior success in the ledger without going through git (the
    # ledger is NOT what decides — disk truth survives restarts)
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


def test_has_last_good_true_when_heads_only_in_packed_refs(tmp_path):
    """After ADR-0024 offline `git gc`, heads live in packed-refs and loose
    refs/heads is empty. A process restart clears the in-process ledger /
    _synced_mono, so on-disk packed heads must still count as last-good."""
    cache, _, _ = make_cache(tmp_path, [R1])
    p = cache.mirror_path("alpha")
    p.mkdir(parents=True)
    (p / "refs" / "heads").mkdir(parents=True)
    (p / "HEAD").write_text("ref: refs/heads/main\n")
    (p / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled\n"
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa refs/heads/main\n"
    )
    assert not any((p / "refs" / "heads").iterdir())
    assert cache.ledger == {} and cache._synced_mono == {}
    assert cache.has_last_good("alpha") is True
    # empty packed-refs / no heads lines must stay False (bare-init case)
    (p / "packed-refs").write_text("# pack-refs with: peeled fully-peeled\n")
    assert cache.has_last_good("alpha") is False


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


def test_rename_mirror_moves_dir_and_ledger(tmp_path):
    cache, _, _ = make_cache(tmp_path, [R1])
    src = cache.mirror_path("alpha")
    src.mkdir(parents=True)
    (src / "HEAD").write_text("ref: refs/heads/main\n")
    from devcake.domain.repo_mirror import MirrorStatus
    cache.ledger["alpha"] = MirrorStatus(ok=True)
    cache._synced_mono["alpha"] = 1.0
    cache.rename_mirror("alpha", "beta")
    assert not src.exists()
    assert cache.mirror_path("beta").is_dir()
    assert "beta" in cache.ledger and "alpha" not in cache.ledger
    assert "beta" in cache._synced_mono and "alpha" not in cache._synced_mono
    cache.rename_mirror("missing", "other")               # missing → no-op


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


# ── empty-branch default resolution (the card's "empty = remote default") ────

def _skill_cache(tmp_path, *, script=None):
    from devcake.config import SkillSource

    class Skill(SkillSource):
        @property
        def token(self):  # type: ignore[override]
            return ""

        @property
        def token_ro(self):  # type: ignore[override]
            return "ro-s"

    cache, calls, forges = make_cache(tmp_path, [], script=script)
    cache.config.skill_sources = [
        Skill(name="shelf", url="https://gitlab.com/o/skills")]
    return cache, calls, forges


def test_empty_branch_resolves_head_via_symref_probe(tmp_path):
    def script(args):
        if args[:1] == ["ls-remote"]:
            return GitResult(0, "ref: refs/heads/trunk\tHEAD\nabc\tHEAD\n", "")
        return None
    cache, calls, _ = _skill_cache(tmp_path, script=script)
    st = run_coro(cache.sync_one("shelf"))
    assert st.ok, st.detail
    probe = next(c for c in calls if c[:1] == ["ls-remote"])
    assert "--symref" in probe and probe[-1] == "HEAD"
    head = next(c for c in calls if "symbolic-ref" in c)
    assert head[-1] == "refs/heads/trunk"


def test_empty_branch_probe_error_keeps_stderr_and_latches_auth(tmp_path):
    """A probe ERROR must never read as 'set Branch on the card': its own
    stderr rides the ledger detail so an auth failure latches the breaker
    exactly like a fetch auth failure would."""
    def script(args):
        if args[:1] == ["ls-remote"]:
            return GitResult(128, "", "remote: … returned error: 401")
        return None
    cache, _, forges = _skill_cache(tmp_path, script=script)
    st = run_coro(cache.sync_one("shelf"))
    assert not st.ok and st.auth
    assert "default-branch probe" in st.detail
    assert "shelf" in forges.breakers
    assert forges.breaker_fields["shelf"] == "token_ro"


def test_resolved_branch_missing_from_fetch_fails_loud(tmp_path):
    """symbolic-ref succeeds on a DANGLING ref — a probed default the fetch
    never brought over (changed mid-sync) must not ledger a green sync
    whose clones would check out nothing. Zero branches is the one
    exception: an empty repository advertising an unborn HEAD bootstraps
    on that name (ADR-0024 addendum)."""
    heads = {"out": "refs/heads/master\n"}

    def script(args):
        if args[:1] == ["ls-remote"]:
            return GitResult(0, "ref: refs/heads/main\tHEAD\nabc\tHEAD\n", "")
        if "rev-parse" in args:
            return GitResult(1, "", "")
        if "for-each-ref" in args:
            return GitResult(0, heads["out"], "")
        return None
    cache, calls, _ = _skill_cache(tmp_path, script=script)
    st = run_coro(cache.sync_one("shelf"))
    assert not st.ok
    assert "no such branch" in st.detail and "master" in st.detail
    assert not any("symbolic-ref" in c for c in calls)
    heads["out"] = ""                                    # an empty repository
    st = run_coro(cache.sync_one("shelf"))
    assert st.ok
    assert any("symbolic-ref" in c and c[-1] == "refs/heads/main" for c in calls)


def test_delete_mirror_pops_bookkeeping_even_without_a_dir(tmp_path):
    """A card whose first sync failed before init holds a ledger row but no
    mirror dir — removal must still drop it, or /health keeps a ghost
    failing row for a nonexistent card until restart."""
    from devcake.domain.repo_mirror import MirrorStatus
    cache, _, _ = make_cache(tmp_path, [R1])
    cache.ledger["alpha"] = MirrorStatus(ok=False, detail="init failed")
    cache._synced_mono["alpha"] = 1.0
    assert not cache.mirror_path("alpha").exists()
    cache.delete_mirror("alpha")
    assert "alpha" not in cache.ledger
    assert "alpha" not in cache._synced_mono


def test_dispatch_backed_skill_card_failure_stays_context_governed(tmp_path):
    """ADR-0039 at the dispatch gate: a backed source's sync failure keys by
    its BACKING card; with strict off and last-good content the run proceeds
    on stale cache instead of deferring."""
    from test_activity_repos import _dispatch_setup
    from fakes import FakeInternalForge
    from devcake.domain.model import MissionType

    class BackedCache(GrantingCache):
        def mirror_name_of(self, name):
            return "work" if name == "shelf" else name

        async def ensure_fresh(self, names):
            assert "shelf" not in names          # resolved before the union
            bad = {n: "fetch: down" for n in names if n == "work"}
            return (not bad), bad

        def has_last_good(self, name):
            return name == "work"

        async def tree_head(self, name):
            return "cafe1234"

    mgr, fake, m, launched = _dispatch_setup(tmp_path, FakeInternalForge())
    mgr.config.context_sourcing_strict = False
    mgr.repo_cache = BackedCache()
    dt = mgr.dev_types["senior-dev"]
    dt.skills = ["shelf/tdd"]
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE, dt))
    assert run is not None                       # stale-cache proceed
    assert "work" in run.mirror_repos            # the PHYSICAL gate snapshot


# ── the repository's HEAD is the truth; a wrong pin is loud (ADR-0024 addendum)

def test_blank_repo_card_resolves_head_via_symref_probe_and_verifies_first(tmp_path):
    """A blank repo card (the model default) rides the skill-source path:
    `ls-remote --symref … HEAD` every sync, the branch verified to exist
    BEFORE the HEAD move — never a green ledger over a dangling HEAD."""
    cache, calls, _ = make_cache(tmp_path, [R1])
    assert R1.default_branch == ""
    st = run_coro(cache.sync_one("alpha"))
    assert st.ok
    probe = next(c for c in calls if c[:2] == ["ls-remote", "--symref"])
    assert probe[-1] == "HEAD"
    verify = next(i for i, c in enumerate(calls) if "rev-parse" in c)
    head = next(i for i, c in enumerate(calls) if "symbolic-ref" in c)
    assert verify < head
    assert calls[head][-1] == "refs/heads/main"


def test_pinned_branch_missing_fails_loud_and_leaves_head_alone(tmp_path):
    """A pin the repository does not have fails the sync with both names
    and moves nothing: no symbolic-ref after the failed verification, no
    remote probe for a pin (the failure wording is local)."""
    pinned = Repo(name="alpha", url="https://gitlab.com/o/alpha.git",
                  default_branch="main", fake_token="rw-a")

    def script(args):
        if "rev-parse" in args:
            return GitResult(1, "", "")
        if "for-each-ref" in args:
            return GitResult(0, "refs/heads/master\nrefs/heads/f/x\n", "")
        return None
    cache, calls, _ = make_cache(tmp_path, [pinned], script=script)
    st = run_coro(cache.sync_one("alpha"))
    assert not st.ok
    assert "pins 'main'" in st.detail and "master" in st.detail
    assert "blank the card's Branch" in st.detail
    assert not any("symbolic-ref" in c for c in calls)
    assert not any(c[:2] == ["ls-remote", "--symref"] for c in calls)
    assert not st.auth                       # a config problem, not a breaker


def test_pin_on_an_empty_repository_syncs_green(tmp_path):
    """Zero heads = a repository awaiting its first commit: the pin keeps
    its HEAD (the claims bootstrap and a first EXECUTE depend on it)."""
    pinned = Repo(name="alpha", url="https://gitlab.com/o/alpha.git",
                  default_branch="main", fake_token="rw-a")

    def script(args):
        if "rev-parse" in args:
            return GitResult(1, "", "")
        if "for-each-ref" in args:
            return GitResult(0, "", "")
        return None
    cache, calls, _ = make_cache(tmp_path, [pinned], script=script)
    st = run_coro(cache.sync_one("alpha"))
    assert st.ok
    assert any(c[-1] == "refs/heads/main" and "symbolic-ref" in c for c in calls)


def test_probed_default_the_fetch_never_brought_fails(tmp_path):
    """Blank card, remote HEAD names `main`, the mirror has branches but
    not that one (the default changed mid-sync): fail, name the branches."""
    def script(args):
        if "rev-parse" in args:
            return GitResult(1, "", "")
        if "for-each-ref" in args:
            return GitResult(0, "refs/heads/master\nrefs/heads/dev\n", "")
        return None
    cache, calls, _ = make_cache(tmp_path, [R1], script=script)
    st = run_coro(cache.sync_one("alpha"))
    assert not st.ok
    assert "remote's HEAD names 'main'" in st.detail and "master, dev" in st.detail
    assert not any("symbolic-ref" in c for c in calls)


def test_resolved_branch_is_pin_then_a_resolving_head_never_bare_init(tmp_path):
    pinned = Repo(name="beta", url="https://gitlab.com/o/beta.git",
                  default_branch="release", fake_token="rw-b")
    cache, _, _ = make_cache(tmp_path, [R1, pinned])
    assert cache.resolved_branch("beta") == "release"
    # blank card: nothing on disk → unresolved
    assert cache.resolved_branch("alpha") == ""
    p = cache.mirror_path("alpha")
    (p / "refs" / "heads").mkdir(parents=True)
    (p / "HEAD").write_text("ref: refs/heads/main\n")      # bare-init shape
    assert cache.resolved_branch("alpha") == ""            # target missing
    (p / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    assert cache.resolved_branch("alpha") == "main"
    # packed refs after an offline gc count too
    (p / "refs" / "heads" / "main").unlink()
    (p / "packed-refs").write_text("# pack-refs with: peeled\n"
                                   + "b" * 40 + " refs/heads/main\n")
    assert cache.resolved_branch("alpha") == "main"
    # a branch with a slash resolves through the loose path
    (p / "HEAD").write_text("ref: refs/heads/feat/x\n")
    (p / "refs" / "heads" / "feat").mkdir()
    (p / "refs" / "heads" / "feat" / "x").write_text("c" * 40 + "\n")
    assert cache.resolved_branch("alpha") == "feat/x"
    assert NullRepoCache().resolved_branch("alpha") == ""


def test_remote_default_branch_parses_the_symref_and_surfaces_errors(tmp_path):
    seen = []

    def script(args):
        if args[:2] == ["ls-remote", "--symref"]:
            seen.append(args)
            return GitResult(0, "ref: refs/heads/trunk\tHEAD\n"
                                "0" * 40 + "\tHEAD\n", "")
        return None
    cache, _, _ = make_cache(tmp_path, [R1], script=script)
    assert run_coro(cache.remote_default_branch("alpha")) == "trunk"
    assert seen[0][2] == "https://oauth2@gitlab.com/o/alpha.git"

    def broken(args):
        if args[:2] == ["ls-remote", "--symref"]:
            # credentials ride askpass, never the URL — git's own wording
            return GitResult(128, "", "fatal: Authentication failed for "
                                      "'https://oauth2@gitlab.com/o/alpha.git'")
        return None
    cache2, _, _ = make_cache(tmp_path / "b", [R1], script=broken)
    with pytest.raises(RuntimeError) as ex:
        run_coro(cache2.remote_default_branch("alpha"))
    assert "Authentication failed" in str(ex.value)
    assert "(timeout)" not in str(ex.value)

    def headless(args):
        if args[:2] == ["ls-remote", "--symref"]:
            return GitResult(0, "0" * 40 + "\tHEAD\n", "")   # no symref line
        return None
    cache3, _, _ = make_cache(tmp_path / "c", [R1], script=headless)
    assert run_coro(cache3.remote_default_branch("alpha")) == ""
    assert run_coro(NullRepoCache().remote_default_branch("alpha")) == ""


def test_dispatch_carries_the_resolved_branch_into_env_and_playbook(tmp_path):
    """The Dev's env and the EXECUTE playbook read the RESOLVED branch —
    pin, else the mirror's HEAD — never the raw card field."""
    from test_activity_repos import _dispatch_setup
    from fakes import FakeInternalForge
    from devcake.domain.model import MissionType

    class TrunkCache(GrantingCache):
        def resolved_branch(self, name):
            return "trunk"
    mgr, fake, m, launched = _dispatch_setup(tmp_path, FakeInternalForge())
    mgr.repo_cache = TrunkCache()
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE,
                                mgr.dev_types["senior-dev"]))
    assert run is not None and launched
    assert run.spec_env["DEVCAKE_DEFAULT_BRANCH"] == "trunk"
    assert "origin/trunk" in run.spec_prompt


def test_dispatch_defers_while_a_blank_card_is_unresolved(tmp_path):
    """A blank card whose mirror has not resolved the repository's HEAD
    must never reach the playbook as `origin/` — deferred like the mirror
    gate (no container, no attempt), reason on the missions row."""
    from test_activity_repos import _dispatch_setup
    from fakes import FakeInternalForge
    from devcake.config import RepoInstance
    from devcake.domain.model import MissionType
    mgr, fake, m, launched = _dispatch_setup(tmp_path, FakeInternalForge())
    mgr.forges._inst = RepoInstance(name="main", url="https://github.com/o/r")
    assert mgr.forges._inst.default_branch == ""
    mgr.repo_cache = GrantingCache()                 # resolved_branch → ""
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE,
                                mgr.dev_types["senior-dev"]))
    assert run is None and not launched
    assert "default branch unresolved" in mgr.blocked_reasons[m.pmo_id]


def test_blank_card_on_an_empty_repository_bootstraps_main(tmp_path):
    """No HEAD symref and no branches = an empty repository: the mirror's
    HEAD takes the bootstrap name so the first push creates it (a fresh
    work repo or notebook must not be a permanent deferral)."""
    from devcake.domain.repo_mirror import BOOTSTRAP_BRANCH

    def script(args):
        if args[:2] == ["ls-remote", "--symref"]:
            return GitResult(0, "", "")                  # nothing advertised
        if "rev-parse" in args:
            return GitResult(1, "", "")
        if "for-each-ref" in args:
            return GitResult(0, "", "")
        return None
    cache, calls, _ = make_cache(tmp_path, [R1], script=script)
    st = run_coro(cache.sync_one("alpha"))
    assert st.ok, st.detail
    assert any("symbolic-ref" in c and c[-1] == f"refs/heads/{BOOTSTRAP_BRANCH}"
               for c in calls)
    # green sync over zero branches: the resolver serves the bootstrap name
    # (a dispatch on a brand-new repository is the first commit); a
    # never-synced bare-init HEAD still resolves to nothing
    assert cache.resolved_branch("alpha") == BOOTSTRAP_BRANCH
    p = cache.mirror_path("alpha")
    # an empty ref directory left by pruning is not a head; packed refs are
    (p / "refs" / "heads" / "feat").mkdir(parents=True, exist_ok=True)
    assert cache.resolved_branch("alpha") == BOOTSTRAP_BRANCH   # still zero heads
    (p / "packed-refs").write_text("a" * 40 + " refs/heads/other\n")
    assert cache.resolved_branch("alpha") == ""      # a head exists, HEAD dangles
    (p / "packed-refs").unlink()
    cache.ledger.pop("alpha")
    assert cache.resolved_branch("alpha") == ""
    # a populated repository that advertises no symref still asks for a pin
    def populated(args):
        if args[:2] == ["ls-remote", "--symref"]:
            return GitResult(0, "", "")
        if "for-each-ref" in args:
            return GitResult(0, "refs/heads/master\n", "")
        return None
    cache2, _, _ = make_cache(tmp_path / "b", [R1], script=populated)
    st = run_coro(cache2.sync_one("alpha"))
    assert not st.ok and "set Branch on the card" in st.detail


def test_changed_pin_invalidates_the_freshness_window(tmp_path):
    """Within sync_max_age_seconds a re-pinned card must resync: the env
    and playbook would carry the new pin while the mirror serves the old."""
    pinned = Repo(name="alpha", url="https://gitlab.com/o/alpha.git",
                  default_branch="main", fake_token="rw-a")
    cache, calls, forges = make_cache(tmp_path, [pinned], max_age=3600)
    assert run_coro(cache.ensure_fresh(["alpha"])) == (True, {})
    n_fetch = sum(1 for c in calls if "fetch" in c)
    assert run_coro(cache.ensure_fresh(["alpha"])) == (True, {})
    assert sum(1 for c in calls if "fetch" in c) == n_fetch     # window held
    pinned.default_branch = "release"
    assert run_coro(cache.ensure_fresh(["alpha"])) == (True, {})
    assert sum(1 for c in calls if "fetch" in c) == n_fetch + 1  # resynced
    assert (cache.mirror_path("alpha") / "HEAD").read_text().strip() \
        == "ref: refs/heads/release"


def test_pinned_card_first_fetch_fails_then_succeeds(tmp_path):
    """A failed first fetch returns before the HEAD block: red ledger, no
    resolvable HEAD, nothing last-good; the next sync resolves normally."""
    pinned = Repo(name="alpha", url="https://gitlab.com/o/alpha.git",
                  default_branch="main", fake_token="rw-a")
    state = {"fail": True}

    def script(args):
        if "fetch" in args and state["fail"]:
            return GitResult(128, "", "fatal: unable to access")
        return None
    cache, calls, _ = make_cache(tmp_path, [pinned], script=script)
    st = run_coro(cache.sync_one("alpha"))
    assert not st.ok
    assert not cache.has_last_good("alpha")
    assert cache.resolved_branch("alpha") == "main"      # the pin, unverified
    state["fail"] = False
    assert run_coro(cache.sync_one("alpha")).ok
    (cache.mirror_path("alpha") / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (cache.mirror_path("alpha") / "refs" / "heads" / "main").write_text("a" * 40)
    assert cache.has_last_good("alpha")


def test_backed_skill_source_with_a_blank_backing_card_resolves_its_head(tmp_path):
    """A repo-backed skill source with no pin of its own reads the backing
    card's resolved branch; its own pin still wins."""
    from devcake.config import SkillSource
    cache, _, _ = make_cache(tmp_path, [R1])
    cache.config.skill_sources = [
        SkillSource(name="skills", url="", backed_by="alpha"),
        SkillSource(name="pinned", url="", backed_by="alpha",
                    default_branch="stable"),
    ]
    p = cache.mirror_path("alpha")
    (p / "refs" / "heads").mkdir(parents=True)
    (p / "HEAD").write_text("ref: refs/heads/trunk\n")
    (p / "refs" / "heads" / "trunk").write_text("a" * 40 + "\n")
    assert cache.resolved_branch("skills") == "trunk"
    assert cache.resolved_branch("pinned") == "stable"
    assert cache.has_last_good("skills")


# ── own-write invalidation (ADR-0024 addendum) ───────────────────────────────

def test_invalidate_drops_freshness_but_keeps_the_ledger_row(tmp_path):
    """Inside a freshness window a dispatch reuses the last sync — unless
    DevCake itself changed the repository: `invalidate` makes the next
    ensure_fresh fetch again while /health's row (last-good) stays."""
    cache, calls, _ = make_cache(tmp_path, [R1], max_age=3600)
    assert run_coro(cache.ensure_fresh(["alpha"])) == (True, {})
    n = sum(1 for c in calls if "fetch" in c)
    assert run_coro(cache.ensure_fresh(["alpha"])) == (True, {})
    assert sum(1 for c in calls if "fetch" in c) == n          # window held
    row = cache.ledger["alpha"]
    cache.invalidate("alpha")
    assert cache.ledger["alpha"] is row and row.ok               # row untouched
    assert run_coro(cache.ensure_fresh(["alpha"])) == (True, {})
    assert sum(1 for c in calls if "fetch" in c) == n + 1        # resynced
    cache.invalidate("nope")                                     # unknown: no-op
    NullRepoCache().invalidate("alpha")


def test_invalidate_resolves_a_backed_skill_source_to_its_physical_mirror(tmp_path):
    from devcake.config import SkillSource
    cache, calls, _ = make_cache(tmp_path, [R1], max_age=3600)
    cache.config.skill_sources = [SkillSource(name="skills", url="", backed_by="alpha")]
    assert run_coro(cache.ensure_fresh(["alpha"])) == (True, {})
    n = sum(1 for c in calls if "fetch" in c)
    cache.invalidate("skills")                     # the backing card's mirror
    assert run_coro(cache.ensure_fresh(["alpha"])) == (True, {})
    assert sum(1 for c in calls if "fetch" in c) == n + 1
