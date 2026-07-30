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
