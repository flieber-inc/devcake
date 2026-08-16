"""Shared forge-issue body conventions (ADR-0034).

GitHub / GitLab / Gitea Issues all encode cancel as a durable footer in
the issue body. Apply and strip live here so a second adapter cannot
invent a global `---` wipe.
"""

from __future__ import annotations

CANCEL_FOOTER = "`devcake:canceled:v1`"

_SEP = "\n\n---\n"
_SEP_SHORT = "\n---\n"


def apply_cancel_footer(body: str) -> str:
    """Append the cancel block once. Idempotent."""
    text = body or ""
    if CANCEL_FOOTER in text:
        return text
    return text.rstrip() + f"{_SEP}{CANCEL_FOOTER}\n"


def strip_cancel_footer(body: str) -> str:
    """Remove only the cancel block. Human `---` rules stay."""
    text = body or ""
    idx = text.find(CANCEL_FOOTER)
    if idx < 0:
        return text
    start = idx
    prefix = text[:idx]
    if prefix.endswith(_SEP):
        start -= len(_SEP)
    elif prefix.endswith(_SEP_SHORT):
        start -= len(_SEP_SHORT)
    end = idx + len(CANCEL_FOOTER)
    if end < len(text) and text[end] == "\n":
        end += 1
    return text[:start] + text[end:]
