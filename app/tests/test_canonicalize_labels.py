"""Managed labels are matched case-insensitively onto ALL_LABELS.

Forge vendors (GitHub, Gitea, GitLab) fold case; a human-created
`Devcake-Plan` must become the domain name so derive()/swap_labels see it.
Unmanaged names keep their stored spelling.
"""

from datetime import datetime, timezone

from devcake.domain.model import (LABEL_OPTIN, LABEL_PLAN, Mission,
                                  canonicalize_labels, derive)


def test_canonicalize_labels_maps_managed_names_case_insensitively():
    assert canonicalize_labels(["Devcake-Plan", "devcake"]) == {
        LABEL_PLAN, LABEL_OPTIN}


def test_canonicalize_labels_keeps_unmanaged_spelling():
    assert canonicalize_labels(["bug", "Needs-QA"]) == {"bug", "Needs-QA"}


def test_canonicalize_labels_drops_empty():
    assert canonicalize_labels(["", "DEVCAKE"]) == {LABEL_OPTIN}


def test_mixed_case_stage_label_is_visible_to_derive():
    """The adapter must emit ALL_LABELS spellings; derive is exact-string."""
    m = Mission(
        pmo_id="1", pmo_kind="issue", key="o/r#1", title="t",
        status="backlog",
        labels=canonicalize_labels(["DEVCAKE", "Devcake-Plan"]),
        updated_at=datetime.now(timezone.utc),
    )
    d = derive(m, "opt_in")
    assert d.mission_type is not None
    assert d.mission_type.value == "PLAN"
    assert "LABEL_CONFLICT" not in d.reason
