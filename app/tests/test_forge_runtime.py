"""ForgeRuntime lifecycle (M10/M11): rebuild reconciles config repos but must
preserve dynamically registered internal-forge entries — every config PUT and
secret PUT/DELETE triggers a rebuild, and wiping the internal registrations
opened a window where zero-repo runspecs failed and REVIEW finalize spuriously
errored until the next poll cycle re-registered them (audit A3)."""

from devcake.config import RepoInstance
from devcake.domain.forge_runtime import ForgeRuntime


def _ext(name="main"):
    return RepoInstance(name=name, url="https://github.com/o/r")


def _internal_inst(name):
    # internal repo names carry hyphens by design — synthesized, not operator input
    return RepoInstance.model_construct(
        name=name, forge="gitea", url=f"http://gitea:3300/devcake-internal/{name}.git",
        default_branch="main", api_base=None)


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
    """The admin Clear endpoint pops forges/instances/internal — a later
    rebuild must not resurrect the entry from a stale carry-over."""
    rt = ForgeRuntime()
    rt.register_internal("linear-t-1", _internal_inst("linear-t-1"), object())
    # what DELETE /api/v1/internal-repos/{name} does:
    rt.forges.pop("linear-t-1", None)
    rt.instances.pop("linear-t-1", None)
    rt.internal.discard("linear-t-1")

    rt.rebuild([_ext()], lambda inst: object())
    assert rt.get("linear-t-1") is None
    assert "linear-t-1" not in rt.internal
