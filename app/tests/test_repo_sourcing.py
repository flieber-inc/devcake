"""repo_sourcing — THE repo-sourcing + blocker-credential rules (ADR-0034;
audit F11). The mirror gate and the runspec extras builder consume one
sourcing list; the ratchet keeps either from growing its own copy back."""

import inspect

from types import SimpleNamespace

import pytest

from devcake.domain.repo_sourcing import (blocker_read_credential,
                                          sourced_repo_names)


def _inst(repos=(), refs=()):
    return SimpleNamespace(repos=list(repos), reference_repos=list(refs))


@pytest.mark.parametrize("mission_type,work,expected", [
    # ONBOARD: routing set + references + blockers, primary first, deduped
    ("ONBOARD", "alpha", ["alpha", "beta", "pub", "int-1", "gone"]),
    # non-ONBOARD stages: references + blockers only
    ("PLAN", "alpha", ["alpha", "pub", "beta", "int-1", "gone"]),
    ("EXECUTE", "beta", ["beta", "pub", "int-1", "gone"]),
    ("REVIEW", "beta", ["beta", "pub", "int-1", "gone"]),
    # STEWARD (ADR-0033 discovery flavor): family work repos ride the
    # blocker-entry shape — and deliberately WITHOUT reference_repos
    ("STEWARD", "alpha", ["alpha", "beta", "int-1", "gone"]),
])
def test_sourcing_table(mission_type, work, expected):
    blockers = [{"repo_ref": "beta"}, {"repo_ref": "int-1"},
                {"repo_ref": "gone"}, {"repo_ref": ""}]
    got = sourced_repo_names(
        work_repo=work, mission_type=mission_type,
        instance=_inst(repos=["alpha", "beta"], refs=["pub"]),
        blocker_entries=blockers)
    assert got == expected


def test_relations_steward_still_sources_primary_only():
    """The relations flavor passes no entries — its sourcing is pinned
    byte-identical to the pre-ADR-0033 behavior: [work_repo], nothing else
    (the mirror gate and the runspec stay in lockstep through the ONE rule)."""
    got = sourced_repo_names(
        work_repo="alpha", mission_type="STEWARD",
        instance=_inst(repos=["alpha", "beta"], refs=["pub"]),
        blocker_entries=None)
    assert got == ["alpha"]


def test_gate_set_is_the_filtered_sourcing_set(tmp_path):
    """The audit's drift scenario made structurally impossible: the mirror
    gate's repo set IS the shared sourcing list under the eligibility
    filter — not a parallel computation."""
    from test_repo_mirror import PUB, R1, R2, make_cache
    cache, _, _ = make_cache(tmp_path, [R1, R2, PUB], internal=["int-1"])
    inst = SimpleNamespace(name="linear", repos=["alpha", "beta"],
                           reference_repos=["pub"])
    blockers = [{"repo_ref": "beta"}, {"repo_ref": "int-1"},
                {"repo_ref": "gone"}]
    for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW", "STEWARD"):
        sourced = sourced_repo_names(work_repo="alpha", mission_type=mt,
                                     instance=inst, blocker_entries=blockers)
        assert cache.needed_for(work_repo="alpha", mission_type=mt,
                                instance=inst, blocker_entries=blockers) \
            == [n for n in sourced if cache.eligible(n)]


def test_blocker_read_credential_rule():
    internal_creds = SimpleNamespace(token_read="tr", clone_url="u",
                                     username="devcake")

    def mgr(internal=None, inst=None, forge=None):
        return SimpleNamespace(
            internal_forge=None if internal is None else SimpleNamespace(
                mission_credentials=lambda name: internal),
            forges=SimpleNamespace(instance=lambda n: inst,
                                   get=lambda n: forge))

    # internal creds win
    kind, creds = blocker_read_credential(
        mgr(internal=internal_creds), "linear-DEV-1")
    assert kind == "internal" and creds.token_read == "tr"
    # configured card: token_ro preferred, write fallback
    card = SimpleNamespace(token_ro="", token="wtok", url="u")
    kind, _i, _f, token = blocker_read_credential(
        mgr(inst=card, forge=object()), "beta")
    assert kind == "configured" and token == "wtok"
    card_ro = SimpleNamespace(token_ro="rtok", token="wtok", url="u")
    assert blocker_read_credential(
        mgr(inst=card_ro, forge=object()), "beta")[3] == "rtok"
    # nothing mountable: no internal creds, no card / no token
    assert blocker_read_credential(mgr(), "gone") is None
    tokenless = SimpleNamespace(token_ro="", token="", url="u")
    assert blocker_read_credential(
        mgr(inst=tokenless, forge=object()), "beta") is None


def test_sourcing_lives_only_in_repo_sourcing():
    """Ratchet (ADR-0034): neither consumer may grow its own sourcing back —
    both must call the chokepoint and neither may touch the sourcing fields
    (instance.repos / reference_repos / blocker repo_ref extraction)."""
    from devcake.domain.orchestrator import dispatch
    from devcake.domain.repo_mirror import RepoCache
    for fn in (RepoCache.needed_for, dispatch._extra_repos_for):
        src = inspect.getsource(fn)
        assert "sourced_repo_names" in src, (
            f"{fn.__qualname__} must source through the chokepoint")
        assert "reference_repos" not in src, (
            f"{fn.__qualname__} re-implements sourcing")
    assert "blocker_read_credential" in inspect.getsource(
        dispatch._blocker_mount_ok)
