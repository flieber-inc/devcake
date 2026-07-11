"""Human-readable run ids (docs/02 §7): Dagu charset ^[-a-zA-Z0-9_]+$, ≤64."""
import re

from devcake.ids import make_run_id


def test_format_and_charset():
    rid = make_run_id("DEV-142", 3, "EXECUTE")
    assert rid.startswith("DEV-142-3-EXECUTE-") and len(rid) <= 64
    assert re.fullmatch(r"[-a-zA-Z0-9_]+", rid)


def test_hostile_key_sanitized():
    rid = make_run_id("PRJ weird/key!!", 1, "ONBOARD")
    assert re.fullmatch(r"[-a-zA-Z0-9_]+", rid) and len(rid) <= 64


def test_uniqueness():
    assert len({make_run_id("A", 1, "PLAN") for _ in range(50)}) == 50
