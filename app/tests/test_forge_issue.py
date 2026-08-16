"""Forge-issue cancel footer — one apply/strip, not a global --- wipe."""

from devcake.adapters.forge_issue import (
    CANCEL_FOOTER, apply_cancel_footer, strip_cancel_footer)


def test_apply_is_idempotent_and_uses_the_separator_block():
    body = apply_cancel_footer("shipped the thing")
    assert body.endswith(f"\n\n---\n{CANCEL_FOOTER}\n")
    assert apply_cancel_footer(body) == body


def test_strip_inverts_apply_and_keeps_human_horizontal_rules():
    original = (
        "Intro\n\n"
        "---\n\n"
        "Section two with a rule above\n\n"
        "---\n\n"
        "Closing"
    )
    posted = apply_cancel_footer(original)
    assert posted.count("---") == 3
    restored = strip_cancel_footer(posted)
    assert CANCEL_FOOTER not in restored
    assert restored.count("---") == 2
    assert "Section two with a rule above" in restored
    assert restored.rstrip() == original.rstrip()


def test_strip_without_footer_is_a_no_op():
    body = "plain\n\n---\n\ntext"
    assert strip_cancel_footer(body) == body


def test_three_adapters_share_the_same_footer_constant():
    from devcake.adapters.gitea_issues.mapping import CANCEL_FOOTER as ge
    from devcake.adapters.github_issues.mapping import CANCEL_FOOTER as gh
    from devcake.adapters.gitlab_issues.mapping import CANCEL_FOOTER as gl
    assert gh is ge is gl is CANCEL_FOOTER
