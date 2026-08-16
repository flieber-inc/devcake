"""Pure forge-issue status/key mapping for GitLab Issues PMO (docs/05).

Independent expected values — no re-deriving production helpers in asserts.
"""

from devcake.adapters.gitlab_issues.mapping import (
    CANCEL_FOOTER,
    mission_key,
    normalize_priority,
    normalize_status,
    parse_team_ref,
    project_path_encoded,
)


def test_parse_team_ref_owner_repo():
    assert parse_team_ref("mygroup/missions") == "mygroup/missions"
    assert parse_team_ref("  org/sub/board  ") == "org/sub/board"


def test_parse_team_ref_rejects_bad_shapes():
    for bad in ("", "solo", "/x"):
        try:
            parse_team_ref(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_project_path_encoded_encodes_slashes():
    assert project_path_encoded("org/sub/board") == "org%2Fsub%2Fboard"


def test_mission_key_format():
    assert mission_key("mygroup/missions", 17) == "mygroup/missions#17"


def test_normalize_status_opened_is_always_backlog():
    assert normalize_status("opened", "") == "backlog"
    assert normalize_status("opened", "body with stuff") == "backlog"


def test_normalize_status_closed_is_done_without_cancel_footer():
    assert normalize_status("closed", "finished work") == "done"
    assert normalize_status("closed", "") == "done"


def test_normalize_status_closed_with_cancel_footer_is_canceled():
    body = f"oops\n\n---\n{CANCEL_FOOTER}\n"
    assert normalize_status("closed", body) == "canceled"


def test_normalize_priority_always_medium():
    assert normalize_priority("urgent") == "medium"
