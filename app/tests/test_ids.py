"""Human-readable run ids (docs/02 §7): Dagu charset ^[-a-zA-Z0-9_]+$, ≤64."""
import re

import pytest

from devcake.domain.ids import RunIdOverflow, make_run_id


def test_format_and_charset():
    rid = make_run_id("linear", "DEV-142", 3, "EXECUTE")
    assert rid.startswith("LINEAR-DEV-142-3-EXECUTE-") and len(rid) <= 64
    assert re.fullmatch(r"[-a-zA-Z0-9_]+", rid)


def test_hostile_key_sanitized():
    rid = make_run_id("linear", "PRJ weird/key!!", 1, "ONBOARD")
    assert re.fullmatch(r"[-a-zA-Z0-9_]+", rid) and len(rid) <= 64


def test_uniqueness():
    assert len({make_run_id("linear", "A", 1, "PLAN") for _ in range(50)}) == 50


def test_overflow_raises_domain_error_not_assert():
    """A forged/overflowed feed marker can inflate seq without bound; the
    64-char Dagu budget must fail as a catchable domain error (dispatch gates
    the ONE mission), never a bare assert — which `python -O` would silently
    disable, shipping an over-long id into Dagu/Docker/Redis."""
    with pytest.raises(RunIdOverflow):
        make_run_id("linear", "DEV-1", int("9" * 39), "EXECUTE")


def test_instance_prefix_separates_colliding_missions():
    """M9 exit criterion at the primitive level: the same PMO identifier in
    two instances yields distinct branches, run ids, and therefore distinct
    ACL users (dev-{run_id}) and container names."""
    from devcake.ports.forge import mission_branch
    assert mission_branch("linteama", "DEV-17") != mission_branch("linteamb", "DEV-17")
    a = make_run_id("linteama", "DEV-17", 1, "EXECUTE")
    b = make_run_id("linteamb", "DEV-17", 1, "EXECUTE")
    assert a.startswith("LINTEAMA-DEV-17-1-EXECUTE-")
    assert b.startswith("LINTEAMB-DEV-17-1-EXECUTE-")


def test_long_underscored_instance_keeps_branch_full_and_run_id_budget():
    """Widened instance names (CAKE-151): branches use the full uppercased
    identity; run ids keep a 12-char scrubbed prefix and stay ≤64."""
    from devcake.ports.forge import mission_branch

    name = "acme_engineering_board"  # 22 chars, underscore
    assert mission_branch(name, "DEV-1") == "devcake/ACME_ENGINEERING_BOARD-DEV-1"
    rid = make_run_id(name, "DEV-1", 1, "EXECUTE")
    assert rid.startswith("ACME_ENGINEE-DEV-1-1-EXECUTE-")
    assert len(rid) <= 64
    assert re.fullmatch(r"[-a-zA-Z0-9_]+", rid)
    # Max-length identity (39): prefix is still first 12 scrubbed upper
    long39 = "a" + ("b" * 38)
    rid39 = make_run_id(long39, "DEV-1", 1, "PLAN")
    assert rid39.startswith("A" + ("B" * 11) + "-DEV-1-1-PLAN-")
    assert len(rid39) <= 64
    # Legacy short names unchanged
    assert make_run_id("linear", "DEV-1", 1, "EXECUTE").startswith(
        "LINEAR-DEV-1-1-EXECUTE-")
