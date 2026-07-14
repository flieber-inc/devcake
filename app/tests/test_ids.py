"""Human-readable run ids (docs/02 §7): Dagu charset ^[-a-zA-Z0-9_]+$, ≤64."""
import re

from devcake.domain.ids import make_run_id


def test_format_and_charset():
    rid = make_run_id("linear", "DEV-142", 3, "EXECUTE")
    assert rid.startswith("LINEAR-DEV-142-3-EXECUTE-") and len(rid) <= 64
    assert re.fullmatch(r"[-a-zA-Z0-9_]+", rid)


def test_hostile_key_sanitized():
    rid = make_run_id("linear", "PRJ weird/key!!", 1, "ONBOARD")
    assert re.fullmatch(r"[-a-zA-Z0-9_]+", rid) and len(rid) <= 64


def test_uniqueness():
    assert len({make_run_id("linear", "A", 1, "PLAN") for _ in range(50)}) == 50


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
