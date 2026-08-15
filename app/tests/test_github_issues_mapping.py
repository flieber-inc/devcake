"""Pure forge-issue status/key mapping for GitHub Issues PMO (docs/05)."""

from devcake.adapters.github_issues.mapping import (
    CANCEL_FOOTER,
    mission_key,
    normalize_priority,
    normalize_status,
    parse_team_ref,
)


def test_parse_team_ref_owner_repo():
    assert parse_team_ref("myorg/missions") == ("myorg", "missions")


def test_parse_team_ref_rejects_bad_shapes():
    for bad in ("", "solo", "a/b/c"):
        try:
            parse_team_ref(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_mission_key_format():
    assert mission_key("myorg", "missions", 17) == "myorg/missions#17"


def test_normalize_status_open_is_backlog():
    assert normalize_status("open", "") == "backlog"


def test_normalize_status_closed_is_done_without_cancel_footer():
    assert normalize_status("closed", "finished") == "done"


def test_normalize_status_closed_with_cancel_footer_is_canceled():
    assert normalize_status("closed", f"x {CANCEL_FOOTER}") == "canceled"


def test_normalize_priority_always_medium():
    assert normalize_priority("urgent") == "medium"
