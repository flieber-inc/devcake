"""Pure forge-issue status/key mapping for Gitea Issues PMO (docs/05).

Independent expected values — no re-deriving production helpers in asserts.
"""

from devcake.adapters.gitea_issues.mapping import (
    CANCEL_FOOTER,
    mission_key,
    normalize_priority,
    normalize_status,
    parse_team_ref,
)


def test_parse_team_ref_owner_repo():
    assert parse_team_ref("devcake-pmo/missions") == ("devcake-pmo", "missions")
    assert parse_team_ref("  org/board  ") == ("org", "board")


def test_parse_team_ref_rejects_bad_shapes():
    for bad in ("", "solo", "a/b/c", "/x", "x/"):
        try:
            parse_team_ref(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_mission_key_format():
    assert mission_key("devcake-pmo", "missions", 17) == "devcake-pmo/missions#17"


def test_normalize_status_open_is_always_backlog():
    # forge-issue profile: no vendor in_progress; stages ride labels only
    assert normalize_status("open", "") == "backlog"
    assert normalize_status("open", "body with stuff") == "backlog"


def test_normalize_status_closed_is_done_without_cancel_footer():
    assert normalize_status("closed", "finished work") == "done"
    assert normalize_status("closed", "") == "done"


def test_normalize_status_closed_with_cancel_footer_is_canceled():
    body = f"oops\n\n---\n{CANCEL_FOOTER}\n"
    assert normalize_status("closed", body) == "canceled"
    assert normalize_status("closed", f"x {CANCEL_FOOTER} y") == "canceled"


def test_normalize_priority_always_medium_without_vendor_field():
    assert normalize_priority(None) == "medium"
    assert normalize_priority(0) == "medium"
    assert normalize_priority("urgent") == "medium"
