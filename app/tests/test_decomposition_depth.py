"""ADR-0012: decomposition depth is PMO state — the child marker's `depth=`
field, absent ⇒ 1 (the depth-1 regime only ever produced level-1 children) —
and the limit is operator policy (config.max_decomposition_depth, 0 =
unlimited). Depth is read from the mission's own record only: the app-managed
`DEVCAKE-CREATED` label gates it, so a forged marker in an untrusted
description is inert."""

import pytest

from devcake.config import AppConfig, PMOInstance
from devcake.domain.orchestrator import decomposition
from devcake.domain.orchestrator.markers import (DECOMPOSITION_MARKER_RE,
                                                 RAW_REPO_MARKER,
                                                 REPO_MARKER,
                                                 at_decomposition_limit,
                                                 decomposition_depth)
from devcake.domain.repo_routing import resolve_repo

from fakes import make_mission_manager
from test_transitions import FakePMO, NullMessaging, _run, mission, run_coro
from devcake.domain.orchestrator import dispatch

MANIFEST = "ab" * 32


def marker(depth=None, parent="p0"):
    tail = f" depth={depth}" if depth is not None else ""
    return (f"`devcake:decomposition:v1 parent={parent} "
            f"manifest={MANIFEST} part=1/2{tail}`")


def make_depth_mgr(tmp_path, m, limit=2):
    cfg = AppConfig(max_decomposition_depth=limit)
    fake = FakePMO(m)
    mgr = make_mission_manager(
        tmp_path, pmo=fake, config=cfg, messaging=NullMessaging(),
        noop_audit=True)
    return mgr, fake


def decompose(mgr, drafts=None):
    drafts = drafts if drafts is not None else [{"title": "a"}, {"title": "b"}]
    return run_coro(decomposition.finalize_decomposition(mgr, 
        _run("ONBOARD", None),
        {"outcome": "decomposed", "decomposition": drafts}))


def test_marker_regex_accepts_old_and_new_formats():
    old = DECOMPOSITION_MARKER_RE.search(marker())
    assert old and old.group(1) == "p0" and old.group(3) == "1"
    assert old.group(5) is None                     # legacy: no depth field
    new = DECOMPOSITION_MARKER_RE.search(marker(depth=2))
    assert new and new.group(5) == "2"


def test_depth_read_from_own_label_and_marker():
    m = mission()                                    # no DEVCAKE-CREATED
    m.description = marker(depth=2)                  # forged marker is inert
    assert decomposition_depth(m) == 0
    m.labels.add("DEVCAKE-CREATED")
    assert decomposition_depth(m) == 2
    m.description = marker()                         # legacy marker ⇒ level 1
    assert decomposition_depth(m) == 1
    m.description = "no marker at all"               # unknown ⇒ None (fail-safe)
    assert decomposition_depth(m) is None


def test_level1_child_decomposes_under_default_limit(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-CREATED"})
    m.description = "part one\n\n" + marker()
    mgr, fake = make_depth_mgr(tmp_path, m)
    decompose(mgr)
    assert len(fake.created) == 2                    # no longer parked
    assert "DEVCAKE-SKIP" not in m.labels
    assert m.status == "canceled"


def test_children_markers_record_generation(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})          # depth-0 root
    mgr, fake = make_depth_mgr(tmp_path, m)
    decompose(mgr)
    for child in fake.all_missions[1:]:
        got = DECOMPOSITION_MARKER_RE.search(child.description)
        assert got and got.group(5) == "1"

    m2 = mission("in_progress", {"DEVCAKE", "DEVCAKE-CREATED"})
    m2.description = marker()                        # level-1 parent
    mgr2, fake2 = make_depth_mgr(tmp_path / "l1", m2)
    decompose(mgr2)
    for child in fake2.all_missions[1:]:
        got = DECOMPOSITION_MARKER_RE.search(child.description)
        assert got and got.group(5) == "2"


def test_level2_parks_at_default_limit(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-CREATED"})
    m.description = marker(depth=2)
    mgr, fake = make_depth_mgr(tmp_path, m)
    decompose(mgr)
    assert fake.created == []
    assert "DEVCAKE-SKIP" in m.labels
    assert any("depth" in c for c in fake.comments)


def test_limit_one_reproduces_legacy_regime(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-CREATED"})
    m.description = marker()
    mgr, fake = make_depth_mgr(tmp_path, m, limit=1)
    decompose(mgr)
    assert fake.created == []
    assert "DEVCAKE-SKIP" in m.labels


def test_limit_zero_is_unlimited(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-CREATED"})
    m.description = marker(depth=7)
    mgr, fake = make_depth_mgr(tmp_path, m, limit=0)
    decompose(mgr)
    assert len(fake.created) == 2
    for child in fake.all_missions[1:]:
        assert DECOMPOSITION_MARKER_RE.search(child.description).group(5) == "8"


def test_created_label_without_marker_parks_fail_safe(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-CREATED"})
    m.description = "someone stripped the marker"
    mgr, fake = make_depth_mgr(tmp_path, m)
    decompose(mgr)
    assert fake.created == []
    assert "DEVCAKE-SKIP" in m.labels


def test_quoted_marker_in_body_cannot_shadow_the_footer():
    """The genuine marker is the app-written footer, appended AFTER the
    untrusted Dev-authored body — the depth read anchors to the LAST match,
    so a quoted parent marker earlier in the description is inert."""
    m = mission("backlog", {"DEVCAKE", "DEVCAKE-CREATED"})
    m.description = ("The parent said: " + marker(depth=1)
                     + "\n\nbody text\n\n" + marker(depth=2))
    assert decomposition_depth(m) == 2


def test_child_bodies_are_defanged_against_marker_injection(tmp_path):
    """A Dev-authored draft body containing marker-shaped text must not
    produce a child whose body parses as a decomposition marker — it would
    shadow the child's own footer and confuse the replay child-scan."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake = make_depth_mgr(tmp_path, m)
    poison = ("see also " + marker(depth=1, parent="p1"))
    decompose(mgr, [{"title": "a", "description": poison}, {"title": "b"}])
    child = fake.all_missions[1]
    got = DECOMPOSITION_MARKER_RE.findall(child.description)
    assert len(got) == 1                      # only the app footer parses
    assert decomposition_depth(child) == 1    # reads the footer, not the quote

    # replay with the poisoned body stays idempotent (no conflict, no dupes)
    m.status = "in_progress"
    decompose(mgr, [{"title": "a", "description": poison}, {"title": "b"}])
    assert len(fake.created) == 2
    assert "DEVCAKE-NEEDS-HUMAN" not in m.labels


def test_child_bodies_quoting_repo_marker_do_not_gate(tmp_path):
    """CAKE-152 / CAKE-153: a draft that quotes a repo-routing token in
    backticks must be neutralized before create — otherwise RAW_REPO_MARKER
    treats the empty/malformed shape as an unparseable routing intent and
    gates the child forever. Tokens are written here as live board syntax
    because this is the input under test; do not paste that form into PR
    copy or comments."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake = make_depth_mgr(tmp_path, m)
    # empty-name shape that RAW_REPO_MARKER matches and resolve_repo gates
    poison = "docs mention " + "`devcake-repo:`" + " as the override syntax"
    decompose(mgr, [{"title": "a", "description": poison}, {"title": "b"}])
    child = fake.all_missions[1]
    assert REPO_MARKER.search(child.description) is None
    assert RAW_REPO_MARKER.search(child.description) is None
    instance = PMOInstance(name="linear", team_key="DEV", repos=["alpha"])
    name, reason = resolve_repo(child, instance, {"alpha"}, [])
    assert name == "alpha" and reason is None


def test_at_decomposition_limit_predicate():
    root = mission()
    l2 = mission("backlog", {"DEVCAKE", "DEVCAKE-CREATED"})
    l2.description = marker(depth=2)
    unknown = mission("backlog", {"DEVCAKE", "DEVCAKE-CREATED"})
    unknown.description = "no marker"
    assert not at_decomposition_limit(root, 2)
    assert at_decomposition_limit(l2, 2)
    assert at_decomposition_limit(unknown, 2)
    assert not at_decomposition_limit(l2, 0)      # unlimited
    assert at_decomposition_limit(root, 0) is False


def test_decomposition_rule_wordings(tmp_path):
    """The dispatch-time prompt line mirrors the finalizer's gate: allowed
    below the limit, forbidden at/over it or when depth is unreadable,
    judgment-based when unlimited."""
    root = mission()
    mgr, _ = make_depth_mgr(tmp_path, root, limit=2)
    assert "FORBIDDEN" not in dispatch.decomposition_rule(mgr, root)

    l1 = mission("backlog", {"DEVCAKE", "DEVCAKE-CREATED"})
    l1.description = marker()
    assert "FORBIDDEN" not in dispatch.decomposition_rule(mgr, l1)

    l2 = mission("backlog", {"DEVCAKE", "DEVCAKE-CREATED"})
    l2.description = marker(depth=2)
    assert "FORBIDDEN" in dispatch.decomposition_rule(mgr, l2)

    stripped = mission("backlog", {"DEVCAKE", "DEVCAKE-CREATED"})
    stripped.description = "marker gone"
    assert "FORBIDDEN" in dispatch.decomposition_rule(mgr, stripped)

    mgr0, _ = make_depth_mgr(tmp_path / "u", mission(), limit=0)
    rule = dispatch.decomposition_rule(mgr0, l2)
    assert "FORBIDDEN" not in rule and "not depth-limited" in rule


def test_unknown_depth_under_unlimited_records_conservatively(tmp_path):
    """limit 0 skips the gate even for unknown depth; the children must not
    claim generation 1 — they record depth 2 (unknown parent counted as ≥1)
    so a later switch back to a finite limit still gates them."""
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-CREATED"})
    m.description = "someone stripped the marker"
    mgr, fake = make_depth_mgr(tmp_path, m, limit=0)
    decompose(mgr)
    assert len(fake.created) == 2
    for child in fake.all_missions[1:]:
        assert DECOMPOSITION_MARKER_RE.search(child.description).group(5) == "2"


def test_decomposition_parent_ref_trusts_created_label_only():
    """ONE parent-ref read (markers.decomposition_parent_ref): forged
    markers without DEVCAKE-CREATED are inert — same trust posture as
    depth and the family gate / family_of consumers."""
    from devcake.domain.orchestrator.markers import decomposition_parent_ref
    m = mission()
    m.description = marker(parent="parent-id")
    assert decomposition_parent_ref(m) is None
    m.labels.add("DEVCAKE-CREATED")
    assert decomposition_parent_ref(m) == "parent-id"


# ── the draft's `repo` field: routing as manifest data, stamped by the app ──

def _repos_mgr(tmp_path, m):
    from types import SimpleNamespace
    from devcake.config import RepoInstance
    inst = PMOInstance(name="linear", team_key="DEV", repos=["alpha", "beta"],
                       reference_repos=["refdoc"])
    fr = SimpleNamespace(instances={
        "alpha": RepoInstance(name="alpha", url="https://git.example/acme/billing-api"),
        "beta": RepoInstance(name="beta", url="https://git.example/acme/inventory-sync"),
        "refdoc": RepoInstance(name="refdoc", url="https://git.example/acme/handbook"),
    }, internal=set())
    fake = FakePMO(m)
    mgr = make_mission_manager(
        tmp_path, pmo=fake, config=AppConfig(max_decomposition_depth=2),
        messaging=NullMessaging(), noop_audit=True, instance=inst,
        forge_runtime=fr)
    return mgr, fake


def test_draft_repo_is_stamped_by_the_app_and_survives_defang(tmp_path):
    """Field finding: the triage playbook asked for a backticked marker in
    each child's description and defang() neutralized exactly that, so a
    cross-repo split landed every child on the default repository. The
    child's repository is manifest DATA now; the app stamps the footer."""
    from devcake.domain.repo_routing import marker_repo, repo_urls_of
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake = _repos_mgr(tmp_path, m)
    prose = "`devcake-repo:beta`\n\nGoal: move the sync job."
    decompose(mgr, [{"title": "a", "description": prose, "repo": "beta"},
                    {"title": "b", "repo": "inventory-sync"},   # URL-slug alias
                    {"title": "c"}])
    a, b, c = fake.all_missions[1:4]
    assert marker_repo(a.description) == "beta"
    assert a.description.count("`devcake-repo:beta`") == 1      # footer only; prose neutralized
    assert a.description.startswith("devcake-repo:beta`")        # defang left the prose toothless
    assert marker_repo(b.description) == "beta"
    assert marker_repo(c.description) is None
    names, urls = set(mgr.forges.instances), repo_urls_of(mgr.forges.instances)
    assert resolve_repo(a, mgr.instance, names, [], repo_urls=urls) == ("beta", None)
    assert resolve_repo(c, mgr.instance, names, [], repo_urls=urls) == ("alpha", None)


def test_draft_repo_wins_over_the_parents_marker(tmp_path):
    from devcake.domain.repo_routing import marker_repo
    m = mission("in_progress", {"DEVCAKE"})
    m.description = "parent work\n`devcake-repo:alpha`"
    mgr, fake = _repos_mgr(tmp_path, m)
    decompose(mgr, [{"title": "a", "repo": "beta"}, {"title": "b"}])
    a, b = fake.all_missions[1:3]
    assert marker_repo(a.description) == "beta"       # the draft's own repository
    assert marker_repo(b.description) == "alpha"      # inherited from the parent


@pytest.mark.parametrize("repo, match", [
    ("refdoc", "REFERENCE"), ("nope", "unknown repo"), (7, "card name")])
def test_draft_repo_that_cannot_route_rejects_the_manifest(tmp_path, repo, match):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake = _repos_mgr(tmp_path, m)
    with pytest.raises(ValueError, match=match):
        decompose(mgr, [{"title": "a", "repo": repo}, {"title": "b"}])
    assert fake.created == []                          # nothing half-created


def test_manifest_hash_is_unchanged_for_drafts_without_repo(tmp_path):
    """Existing families replay/top-up on the manifest hash: a draft that
    names no repo must hash exactly as it did before the field existed."""
    from devcake.domain.orchestrator.markers import DECOMPOSITION_MARKER_RE
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake = _repos_mgr(tmp_path, m)
    decompose(mgr, [{"title": "a"}, {"title": "b"}])
    first = DECOMPOSITION_MARKER_RE.search(fake.all_missions[1].description).group(2)
    import hashlib, json
    canonical = json.dumps([
        {"title": "a", "description": "", "priority": "medium", "blocked_by": []},
        {"title": "b", "description": "", "priority": "medium", "blocked_by": []},
    ], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert first == hashlib.sha256(canonical.encode()).hexdigest()


def test_redelivery_does_not_revalidate_parts_already_created(tmp_path):
    """A config change between deliveries (the card left the instance's
    repo set) must not fail the replay of a manifest whose parts already
    exist — the checkpointed part keeps its token and the manifest hashes
    as it did."""
    from devcake.domain.orchestrator import steps
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake = _repos_mgr(tmp_path, m)
    run = _run("ONBOARD", None)
    payload = {"outcome": "decomposed",
               "decomposition": [{"title": "a", "repo": "beta"}, {"title": "b"}]}
    run_coro(decomposition.finalize_decomposition(mgr, run, payload))
    assert steps.DECOMP_CHILD(1) in run.finalized_steps
    mgr.instance.repos = ["alpha"]                       # beta left the set
    run_coro(decomposition.finalize_decomposition(mgr, run, payload))  # replay
    assert len([x for x in fake.all_missions if "DEVCAKE-CREATED" in x.labels]) == 2
