"""ForgeRuntime lifecycle (M10/M11): rebuild reconciles config repos but must
preserve dynamically registered internal-forge entries — every config PUT and
secret PUT/DELETE triggers a rebuild, and wiping the internal registrations
opened a window where zero-repo runspecs failed and REVIEW finalize spuriously
errored until the next poll cycle re-registered them (audit A3)."""

import asyncio

from devcake.config import RepoInstance
from devcake.domain.forge_runtime import ForgeRuntime
from devcake.ports.forge import ForgeHealth


def run_coro(c):
    # the suite's shared idiom (test_forge.py): run_until_complete on the
    # persistent loop — asyncio.run() would CLOSE it for later test files
    return asyncio.get_event_loop().run_until_complete(c)


def _ext(name="main"):
    return RepoInstance(name=name, url="https://github.com/o/r")


def _internal_inst(name):
    # internal repo names carry hyphens by design — synthesized, not operator input.
    # Always auto-merge (ADR-0020) — match dispatch.resolve_repo_live provision.
    return RepoInstance.model_construct(
        name=name, forge="gitea", url=f"http://gitea:3300/devcake-internal/{name}.git",
        default_branch="main", api_base=None,
        auto_merge=True, auto_resolve_merge_conflicts=True,
        merge_retry_window_minutes=30)


def test_rebuild_preserves_internal_registrations():
    rt = ForgeRuntime()
    rt.rebuild([_ext()], lambda inst: ("v1", inst.name))
    inst = _internal_inst("linear-t-1")
    adapter = object()
    rt.register_internal("linear-t-1", inst, adapter)
    rt.health["linear-t-1"] = {"ok": True, "detail": ""}
    rt.latch("linear-t-1", "429 overloaded")

    rt.rebuild([_ext()], lambda i: ("v2", i.name))    # any config/secret PUT

    assert rt.get("linear-t-1") is adapter
    assert rt.instance("linear-t-1") is inst
    assert rt.health["linear-t-1"] == {"ok": True, "detail": ""}
    assert rt.breakers["linear-t-1"] == "429 overloaded"   # latch survives too
    assert "linear-t-1" in rt.internal


def test_rebuild_still_drops_removed_external_repos():
    rt = ForgeRuntime()
    rt.rebuild([_ext("alpha"), _ext("beta")], lambda inst: object())
    rt.health["beta"] = {"ok": False, "transient": False, "detail": "401"}
    rt.latch("beta", "401")

    rt.rebuild([_ext("alpha")], lambda inst: object())

    assert rt.get("beta") is None
    assert "beta" not in rt.health and "beta" not in rt.breakers
    assert rt.get("alpha") is not None


def test_removed_repo_card_leaves_no_forge_or_mirror_ghost(tmp_path):
    """CAKE-147: operator deletion of a config repo card — ForgeRuntime.rebuild
    drops health/breakers, and delete_mirror pops the ledger — so /health
    circuit_breakers `repo:` rows and repo_mirror.mirrors name nothing."""
    from devcake.config import AppConfig
    from devcake.domain.repo_mirror import MirrorStatus, RepoCache

    alpha = RepoInstance(name="alpha", url="https://github.com/o/alpha")
    beta = RepoInstance(name="beta", url="https://github.com/o/beta")
    rt = ForgeRuntime()
    rt.rebuild([alpha, beta], lambda inst: object())
    rt.health["beta"] = {"ok": False, "transient": False, "detail": "401"}
    rt.latch("beta", "401")

    mirrors = tmp_path / "mirrors"
    mirrors.mkdir()
    cfg = AppConfig(repos=[alpha, beta])
    cache = RepoCache(cfg, rt, root=mirrors, clone_user_of=lambda _fid: "")
    beta_path = cache.mirror_path("beta")
    beta_path.mkdir(parents=True)
    (beta_path / "config").write_text("x")
    cache.ledger["beta"] = MirrorStatus(ok=False, detail="stale")
    cache.ledger["alpha"] = MirrorStatus(ok=True, detail="")

    # same order as config PUT remove: reload rebuilds forges, then removed
    # repo scope best-effort delete_mirror
    live_repos = [alpha]
    cfg.repos = live_repos
    rt.rebuild(live_repos, lambda inst: object())
    cache.delete_mirror("beta")

    assert rt.get("beta") is None
    assert "beta" not in rt.health and "beta" not in rt.breakers
    assert "beta" not in cache.ledger
    assert "beta" not in cache.health_map()
    assert not beta_path.exists()
    assert "alpha" in rt.forges and "alpha" in cache.ledger


def test_deleted_internal_repo_stays_deleted_across_rebuild():
    """Admin Clear (unregister) drops all five maps; a later rebuild must not
    resurrect the entry from a stale carry-over (audit A3 carry-over only
    applies to names still in `internal`)."""
    name = "linear-t-1"
    rt = ForgeRuntime()
    rt.register_internal(name, _internal_inst(name), object())
    # latched state as after a 404 probe on a ghost Gitea repo
    rt.health[name] = {
        "ok": False, "transient": False,
        "detail": "repository access failed (HTTP 404)",
    }
    rt.latch(name, "repository access failed (HTTP 404)")
    assert name in rt.breakers and name in rt.health

    # what DELETE /api/v1/internal-repos/{name} does:
    rt.unregister(name)

    assert rt.get(name) is None
    assert rt.instance(name) is None
    assert name not in rt.internal
    assert name not in rt.health
    assert name not in rt.breakers

    rt.rebuild([_ext()], lambda inst: object())
    assert rt.get(name) is None
    assert name not in rt.internal
    assert name not in rt.health
    assert name not in rt.breakers


def test_delete_internal_repo_service_clears_health_and_breakers():
    """Service seam: DELETE Clear must go through unregister so health and
    breakers do not stick after Gitea/secret cleanup. A regression that only
    pops forges/instances/internal leaves the SPA circuit_breakers alert
    until process restart — this test fails that incomplete Clear."""
    from types import SimpleNamespace

    from devcake.api.internal_repos_service import delete_internal_repo

    name = "linear2-dev-137"
    rt = ForgeRuntime()
    rt.register_internal(name, _internal_inst(name), object())
    rt.health[name] = {
        "ok": False, "transient": False,
        "detail": "repository access failed (HTTP 404); the token needs "
                  "write:repository scope and repo access",
    }
    rt.latch(name, rt.health[name]["detail"])

    deleted: list[str] = []

    class _Forge:
        async def delete_repo(self, repo_name: str) -> None:
            deleted.append(repo_name)

    store = SimpleNamespace(active=lambda: [])
    out = run_coro(delete_internal_repo(
        name, internal_forge=_Forge(), store=store, forge_runtime=rt))

    assert out == {"deleted": name}
    assert deleted == [name]
    assert rt.get(name) is None
    assert rt.instance(name) is None
    assert name not in rt.internal
    assert name not in rt.health
    assert name not in rt.breakers

    # config/secret rebuild must not resurrect (internals only carry names
    # still in `internal`)
    rt.rebuild([_ext()], lambda inst: object())
    assert name not in rt.forges
    assert name not in rt.health
    assert name not in rt.breakers


# ── bounded-parallel refresh_all (incident 2026-08-01) ───────────────────────
# 319 configured repos probed sequentially held FastAPI lifespan (and the
# poll cycle's breaker re-probe) for ~95s+. refresh_all must run probes
# concurrently under a semaphore, and stamp last_full_probe_at so /health
# can distinguish "probe pending" from "probe done".

OK_HEALTH = dict(ok=True, repository="o/x", can_push=True, can_read=True,
                 transient=False, detail="")


def test_refresh_all_runs_probes_concurrently():
    rt = ForgeRuntime()
    started = {"n": 0}
    all_started = asyncio.Event()

    class _Rendezvous:
        async def health_probe(self):
            started["n"] += 1
            if started["n"] == 3:
                all_started.set()
            # deadlocks unless all three probes are in flight at once
            await asyncio.wait_for(all_started.wait(), timeout=2)
            return ForgeHealth(**OK_HEALTH)

    rt.rebuild([_ext(f"r{i}") for i in range(3)], lambda i: _Rendezvous())
    health = run_coro(asyncio.wait_for(rt.refresh_all(), timeout=5))
    assert len(health) == 3
    assert all(h["ok"] for h in health.values())


def test_refresh_all_respects_concurrency_limit():
    rt = ForgeRuntime()
    gauge = {"current": 0, "peak": 0}

    class _Slow:
        async def health_probe(self):
            gauge["current"] += 1
            gauge["peak"] = max(gauge["peak"], gauge["current"])
            for _ in range(3):
                await asyncio.sleep(0)
            gauge["current"] -= 1
            return ForgeHealth(**OK_HEALTH)

    rt.rebuild([_ext(f"r{i}") for i in range(6)], lambda i: _Slow())
    run_coro(rt.refresh_all(limit=2))
    assert gauge["peak"] == 2          # bounded AND actually parallel
    assert len(rt.health) == 6


def test_refresh_all_probe_failure_yields_transient_entry_and_completes_map():
    rt = ForgeRuntime()

    class _Boom:
        async def health_probe(self):
            raise RuntimeError("connection reset")

    def make(inst):
        return _Boom() if inst.name == "bad" else _ProbeForge(**OK_HEALTH)

    rt.rebuild([_ext("good1"), _ext("bad"), _ext("good2")], make)
    health = run_coro(rt.refresh_all())
    assert set(health) == {"good1", "bad", "good2"}
    assert health["good1"]["ok"] and health["good2"]["ok"]
    assert health["bad"]["ok"] is False
    assert health["bad"]["transient"] is True   # never raises, never latches
    assert "bad" not in rt.breakers


def test_refresh_all_stamps_last_full_probe_at():
    rt = ForgeRuntime()
    assert rt.last_full_probe_at is None
    rt.rebuild([_ext("solo")], lambda i: _ProbeForge(**OK_HEALTH))
    run_coro(rt.refresh_all())
    assert rt.last_full_probe_at is not None


# ── reference-only repos (founder decision 2026-07-15, round 2) ──────────────
# A repo storing ONLY a read-only token is a first-class reference-only
# citizen: readable-but-not-writable is its EXPECTED healthy state. It must
# not latch a breaker, and make_forge must build its adapter with the read
# token so probes/reads work at all. A read token that cannot even READ is
# still a real failure and latches normally.

class _ProbeForge:
    def __init__(self, **health):
        self._health = health

    async def health_probe(self):
        return ForgeHealth(**self._health)


READABLE_NOT_WRITABLE = dict(
    ok=False, repository="o/x", can_push=False, can_read=True, transient=False,
    detail="token can read the repository but lacks push permission")


def _store_repo_secrets(tmp_path, monkeypatch, name, **fields):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as s
    for field, value in fields.items():
        s.write_connection_secret("repo", name, field, value)


def test_reference_only_repo_is_healthy_on_readable_probe(tmp_path, monkeypatch):
    _store_repo_secrets(tmp_path, monkeypatch, "docs",
                        token_ro="ro-token-0123456789")
    rt = ForgeRuntime()
    rt.rebuild([RepoInstance(name="docs", url="https://github.com/o/docs")],
               lambda i: _ProbeForge(**READABLE_NOT_WRITABLE))
    data = run_coro(rt.refresh_health("docs"))
    assert data["ok"] is True and data["can_push"] is False
    assert "reference-only" in data["detail"]
    assert "docs" not in rt.breakers


def test_work_repo_lacking_push_still_latches(tmp_path, monkeypatch):
    _store_repo_secrets(tmp_path, monkeypatch, "app",
                        token="write-token-0123456789")
    rt = ForgeRuntime()
    rt.rebuild([RepoInstance(name="app", url="https://github.com/o/app")],
               lambda i: _ProbeForge(**READABLE_NOT_WRITABLE))
    data = run_coro(rt.refresh_health("app"))
    assert data["ok"] is False
    assert "app" in rt.breakers


def test_reference_only_repo_unreadable_probe_still_latches(tmp_path, monkeypatch):
    _store_repo_secrets(tmp_path, monkeypatch, "docs",
                        token_ro="expired-token-0123456789")
    rt = ForgeRuntime()
    rt.rebuild([RepoInstance(name="docs", url="https://github.com/o/docs")],
               lambda i: _ProbeForge(ok=False, repository="o/docs",
                                     can_push=False, can_read=False,
                                     transient=False,
                                     detail="repository access failed (HTTP 401)"))
    data = run_coro(rt.refresh_health("docs"))
    assert data["ok"] is False
    assert "docs" in rt.breakers


def test_reference_only_property(tmp_path, monkeypatch):
    _store_repo_secrets(tmp_path, monkeypatch, "docs", token_ro="r-0123456789")
    _store_repo_secrets(tmp_path, monkeypatch, "both",
                        token="w-0123456789", token_ro="r-0123456789")
    url = "https://github.com/o/x"
    assert RepoInstance(name="docs", url=url).reference_only
    assert not RepoInstance(name="both", url=url).reference_only
    assert not RepoInstance(name="bare", url=url).reference_only   # no tokens


def test_make_forge_reference_only_uses_read_token(tmp_path, monkeypatch):
    _store_repo_secrets(tmp_path, monkeypatch, "docs",
                        token_ro="ro-value-0123456789abcd")
    from devcake.adapters.registry import make_forge
    from devcake.security import unregister_runtime_secret
    inst = RepoInstance(name="docs", url="https://github.com/o/docs",
                        forge="github")
    try:
        forge = make_forge(inst)
        assert forge.token == "ro-value-0123456789abcd"
    finally:
        for key in ("forge_token:docs", "forge_token_ro:docs",
                    "forge_reviewer:docs"):
            unregister_runtime_secret(key)


# ── CAKE-118: credential-keyed breaker clear ────────────────────────────────
# Dual-token notebook cards latch on the read-preferred clone credential
# (token_ro). A write-preferred health probe must NOT clear that latch.


def test_token_ro_latch_stays_when_write_token_probe_ok():
    """Bug reproduction: notebook-clone latch + write-token-only probe success
    must leave the breaker latched (CAKE-118)."""
    rt = ForgeRuntime()
    rt.forges["nb"] = object()
    rt.latch("nb", "repository credential rejected in run-x",
             credential_field="token_ro")
    assert "nb" in rt.breakers
    assert rt.breaker_fields["nb"] == "token_ro"

    rt.apply_health("nb", {"ok": True, "detail": "",
                           "credential_field": "token"})
    assert "nb" in rt.breakers
    assert rt.breaker_fields["nb"] == "token_ro"


def test_token_ro_latch_clears_when_read_token_probe_ok():
    """Self-heal: restored token_ro clears within one matching probe."""
    rt = ForgeRuntime()
    rt.forges["nb"] = object()
    rt.latch("nb", "repository credential rejected in run-x",
             credential_field="token_ro")
    rt.apply_health("nb", {"ok": True, "detail": "",
                           "credential_field": "token_ro"})
    assert "nb" not in rt.breakers
    assert "nb" not in rt.breaker_fields


def test_work_repo_token_latch_clears_on_write_probe_ok():
    """Control: work-repo latch keyed on token still clears on write probe."""
    rt = ForgeRuntime()
    rt.forges["main"] = object()
    rt.latch("main", "repository credential rejected in run-y",
             credential_field="token")
    rt.apply_health("main", {"ok": True, "detail": "",
                             "credential_field": "token"})
    assert "main" not in rt.breakers
    assert "main" not in rt.breaker_fields


def test_single_token_card_latch_clear_unchanged(tmp_path, monkeypatch):
    """Control: single-token (write-only) card latch + matching probe clears."""
    _store_repo_secrets(tmp_path, monkeypatch, "app",
                        token="write-token-0123456789")
    rt = ForgeRuntime()
    rt.rebuild(
        [RepoInstance(name="app", url="https://github.com/o/app")],
        lambda i: _ProbeForge(**OK_HEALTH))
    rt.latch("app", "repository credential rejected in run-z",
             credential_field="token")
    data = run_coro(rt.refresh_health("app"))
    assert data["ok"] is True
    assert data.get("credential_field") == "token"
    assert "app" not in rt.breakers
    assert "app" not in rt.breaker_fields


def test_refresh_health_reprobes_latched_token_ro_not_write(
        tmp_path, monkeypatch):
    """Dual-token card: latched token_ro must re-probe with the read token.
    A write-only-green factory must leave the latch in place."""
    _store_repo_secrets(tmp_path, monkeypatch, "nb",
                        token="write-ok-0123456789abcd",
                        token_ro="read-dead-0123456789ab")
    probed: list[str] = []

    def make(inst, *, credential_field=None):
        # Default write-preferred matches make_forge; field-specific uses
        # exactly that secret (empty when missing).
        if credential_field == "token":
            tok = inst.token
        elif credential_field == "token_ro":
            tok = inst.token_ro
        else:
            tok = inst.token or inst.token_ro
        probed.append(credential_field or "default")

        class _TokProbe:
            async def health_probe(self):
                if tok.startswith("write-ok"):
                    return ForgeHealth(**OK_HEALTH)
                return ForgeHealth(
                    ok=False, repository="o/nb", can_push=False,
                    can_read=False, transient=False,
                    detail="repository access failed (HTTP 401)")

        return _TokProbe()

    rt = ForgeRuntime()
    rt.rebuild([RepoInstance(name="nb", url="https://github.com/o/nb",
                             forge="github")], make)
    rt.latch("nb", "notebook clone auth", credential_field="token_ro")
    data = run_coro(rt.refresh_health("nb"))
    assert "token_ro" in probed
    assert data["ok"] is False
    assert "nb" in rt.breakers
    assert rt.breaker_fields["nb"] == "token_ro"


def test_token_ro_latch_clears_on_readable_without_push(
        tmp_path, monkeypatch):
    """Mission self-heal on dual-token (non-reference_only) cards: a restored
    read-only PAT returns ForgeHealth(ok=False, can_read=True, can_push=False).
    refresh_health must treat that as healthy for a token_ro-keyed latch and
    clear the breaker (REVIEW reject on CAKE-118 round 1)."""
    _store_repo_secrets(tmp_path, monkeypatch, "nb",
                        token="write-ok-0123456789abcd",
                        token_ro="read-ok-0123456789abcd")
    assert not RepoInstance(
        name="nb", url="https://github.com/o/nb").reference_only

    def make(inst, *, credential_field=None):
        return _ProbeForge(**READABLE_NOT_WRITABLE)

    rt = ForgeRuntime()
    rt.rebuild([RepoInstance(name="nb", url="https://github.com/o/nb",
                             forge="github")], make)
    rt.latch("nb", "notebook clone auth", credential_field="token_ro")
    data = run_coro(rt.refresh_health("nb"))
    assert data["ok"] is True
    assert data.get("can_read") is True
    assert data.get("can_push") is False
    assert data.get("credential_field") == "token_ro"
    assert "nb" not in rt.breakers
    assert "nb" not in rt.breaker_fields


def test_token_ro_latch_stays_when_read_probe_unreadable(
        tmp_path, monkeypatch):
    """Negative control: token_ro re-probe that cannot read stays latched."""
    _store_repo_secrets(tmp_path, monkeypatch, "nb",
                        token="write-ok-0123456789abcd",
                        token_ro="read-dead-0123456789ab")

    def make(inst, *, credential_field=None):
        return _ProbeForge(
            ok=False, repository="o/nb", can_push=False, can_read=False,
            transient=False,
            detail="repository access failed (HTTP 401)")

    rt = ForgeRuntime()
    rt.rebuild([RepoInstance(name="nb", url="https://github.com/o/nb",
                             forge="github")], make)
    rt.latch("nb", "notebook clone auth", credential_field="token_ro")
    data = run_coro(rt.refresh_health("nb"))
    assert data["ok"] is False
    assert "nb" in rt.breakers
    assert rt.breaker_fields["nb"] == "token_ro"
