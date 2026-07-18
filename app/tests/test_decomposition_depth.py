"""ADR-0012: decomposition depth is PMO state — the child marker's `depth=`
field, absent ⇒ 1 (the depth-1 regime only ever produced level-1 children) —
and the limit is operator policy (config.max_decomposition_depth, 0 =
unlimited). Depth is read from the mission's own record only: the app-managed
`DEVCAKE-CREATED` label gates it, so a forged marker in an untrusted
description is inert."""

from devcake.config import AppConfig
from devcake.domain.orchestrator.markers import (DECOMPOSITION_MARKER_RE,
                                                 at_decomposition_limit,
                                                 decomposition_depth)

from fakes import make_mission_manager
from test_transitions import FakePMO, NullMessaging, _run, mission, run_coro

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
    return run_coro(mgr._finalize_decomposition(
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
    assert "FORBIDDEN" not in mgr._decomposition_rule(root)

    l1 = mission("backlog", {"DEVCAKE", "DEVCAKE-CREATED"})
    l1.description = marker()
    assert "FORBIDDEN" not in mgr._decomposition_rule(l1)

    l2 = mission("backlog", {"DEVCAKE", "DEVCAKE-CREATED"})
    l2.description = marker(depth=2)
    assert "FORBIDDEN" in mgr._decomposition_rule(l2)

    stripped = mission("backlog", {"DEVCAKE", "DEVCAKE-CREATED"})
    stripped.description = "marker gone"
    assert "FORBIDDEN" in mgr._decomposition_rule(stripped)

    mgr0, _ = make_depth_mgr(tmp_path / "u", mission(), limit=0)
    rule = mgr0._decomposition_rule(l2)
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
