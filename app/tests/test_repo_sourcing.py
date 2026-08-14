"""repo_sourcing — THE repo-sourcing + blocker-credential rules (ADR-0034;
audit F11). The mirror gate and the runspec extras builder consume one
sourcing list; the ratchet keeps either from growing its own copy back."""

import inspect

from types import SimpleNamespace

import pytest

from devcake.domain.repo_sourcing import (blocker_read_credential,
                                          classify_context_failures,
                                          inherited_curator_extras,
                                          memory_mount_names,
                                          sourced_repo_names)


def _inst(repos=(), refs=(), memory=(), name="eng"):
    return SimpleNamespace(name=name, repos=list(repos),
                           reference_repos=list(refs),
                           memory_repos=list(memory))


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


def test_memory_mount_union_dedupes_excludes_repo_ref_and_includes_steward():
    """PLAN_MEMORY §3.2 / F2 / D2: instance then Dev Type, minus repo_ref.
    STEWARD mounts the same union (works discoveries; does not author)."""
    inst = _inst(memory=["nb", "docs"])
    dt = SimpleNamespace(memory_repos=["docs", "nb2"])
    assert memory_mount_names(instance=inst, dev_type=dt,
                              repo_ref="webapp") == ["nb", "docs", "nb2"]
    # F2: a Curator whose Dev Type lists the notebook must not remount it
    assert memory_mount_names(instance=_inst(memory=[]),
                              dev_type=SimpleNamespace(memory_repos=["nb"]),
                              repo_ref="nb") == []
    assert memory_mount_names(instance=_inst(memory=["nb"]),
                              dev_type=dt, repo_ref="nb") == ["docs", "nb2"]
    # no mounts when both lists empty
    assert memory_mount_names(instance=_inst(), dev_type=None,
                              repo_ref="webapp") == []


def test_sourced_includes_memory_on_every_stage_including_steward():
    inst = _inst(repos=["alpha", "beta"], refs=["pub"], memory=["nb"])
    dt = SimpleNamespace(memory_repos=["nb2"])
    blockers = [{"repo_ref": "int-1"}]
    for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW", "STEWARD"):
        got = sourced_repo_names(
            work_repo="alpha", mission_type=mt, instance=inst,
            blocker_entries=blockers, dev_type=dt)
        assert "nb" in got and "nb2" in got
        assert got[0] == "alpha"
        # F2: primary is never a second clone name
        assert got.count("alpha") == 1


def test_sourced_memory_excluded_when_it_is_the_work_repo():
    inst = _inst(repos=["nb"], memory=[])
    dt = SimpleNamespace(memory_repos=["nb"])
    got = sourced_repo_names(
        work_repo="nb", mission_type="EXECUTE", instance=inst,
        blocker_entries=None, dev_type=dt)
    assert got == ["nb"]


def test_inherit_cs_shaped_fixture_not_other_notebooks():
    """PLAN_MEMORY §7: Curator inherit = consumer repos ∪ reference_repos.
    Other memory notebooks and `m` itself stay out."""
    from devcake.config import AppConfig, PMOInstance, RepoInstance
    cfg = AppConfig(
        repos=[RepoInstance(name=n, url=f"https://github.com/acme/{n}")
               for n in ("webapp", "docs", "nb", "othernb")],
        pmos=[
            PMOInstance(name="cs", team_key="A", repos=["webapp"],
                        reference_repos=["docs"], memory_repos=["nb"]),
            PMOInstance(name="cur", team_key="B", repos=["nb"],
                        reference_repos=[]),
            PMOInstance(name="other", team_key="C", repos=["webapp"],
                        memory_repos=["othernb"]),
        ])
    cur = next(p for p in cfg.pmos if p.name == "cur")
    got = inherited_curator_extras(cfg, cur, "nb")
    assert got == ["webapp", "docs"]
    assert "nb" not in got
    assert "othernb" not in got
    # non-curator work repo inherits nothing
    cs = next(p for p in cfg.pmos if p.name == "cs")
    assert inherited_curator_extras(cfg, cs, "webapp") == []


def test_sourced_curator_includes_inherit_and_needed_matches(tmp_path):
    from devcake.config import AppConfig, PMOInstance, RepoInstance
    from test_repo_mirror import PUB, R1, R2, make_cache
    cfg = AppConfig(
        repos=[RepoInstance(name=n, url=f"https://github.com/acme/{n}")
               for n in ("alpha", "beta", "pub", "nb")],
        pmos=[
            PMOInstance(name="cs", team_key="A", repos=["alpha"],
                        reference_repos=["pub"], memory_repos=["nb"]),
            PMOInstance(name="cur", team_key="B", repos=["nb"]),
        ])
    # give the cache cards matching cfg names
    cache, _, _ = make_cache(tmp_path, [R1, R2, PUB], internal=["int-1"])
    cur = next(p for p in cfg.pmos if p.name == "cur")
    sourced = sourced_repo_names(
        work_repo="nb", mission_type="EXECUTE", instance=cur,
        blocker_entries=None, config=cfg)
    assert sourced[0] == "nb"
    assert "alpha" in sourced and "pub" in sourced
    assert cache.needed_for(
        work_repo="nb", mission_type="EXECUTE", instance=cur,
        blocker_entries=None, config=cfg) == [
            n for n in sourced if cache.eligible(n)]


def test_classify_context_failures_strict_and_open():
    why = {"nb": "sync failed", "skillrepo": "404", "alpha": "auth"}
    defer, stale, omit = classify_context_failures(
        why, context_cards={"nb", "skillrepo"}, strict=True,
        has_mirror=lambda n: n == "nb")
    # work-repo failure always defers; context cards defer when strict
    assert "alpha" in defer and "nb" in defer and "skillrepo" in defer
    assert stale == set() and omit == set()
    defer, stale, omit = classify_context_failures(
        why, context_cards={"nb", "skillrepo"}, strict=False,
        has_mirror=lambda n: n == "nb")
    assert defer == {"alpha": "auth"}          # work still fail-closed
    assert stale == {"nb"}                     # last-good mirror
    assert omit == {"skillrepo"}               # never synced


def test_skill_source_cards_is_gate_only_never_sourcing(tmp_path, monkeypatch):
    """Skill cards feed the mirror GATE and the payload — never the clone
    set. A gate snapshot that includes `skillrepo` (145 stamps the union on
    run.mirror_repos) must not make `_extra_repos_for` emit that card."""
    from devcake.domain.orchestrator import dispatch
    from devcake.domain.repo_sourcing import skill_source_cards
    from test_repo_mirror import _execute_run, _extras_rig

    assert skill_source_cards(["skillrepo/tdd", "flat", "other/x"]) == {
        "skillrepo", "other"}
    assert skill_source_cards([]) == set()

    mgr = _extras_rig(tmp_path, monkeypatch)
    # gate snapshot includes the skill card (truthful needed-set union)
    # plus the primary + a real extra (beta is a reference repo)
    run = _execute_run(mirror_repos=["alpha", "beta", "skillrepo"])
    names = [x["name"] for x in dispatch._extra_repos_for(mgr, run)]
    assert "beta" in names
    assert "skillrepo" not in names
    assert "alpha" not in names          # primary is not an extra
