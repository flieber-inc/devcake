"""Required-row matrix. Reviewed like code; the receipt does not choose.

Expected classify triples are the fixture-meta literals
(`devcake_exit_now` / `devcake_class_now` / `observed_reason`).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RowSpec:
    name: str
    required: bool
    expected: dict | None  # {exit, class, reason} for classify rows
    kind: str = "classify"  # classify | flag_accept
    skip_reason: str = ""


# Intended triples from test_harness_captures CAPTURES, not sidecar
# measurements. Sidecars are measured facts; expectations are reviewed there.
_NO_FAULT = {"exit": 11, "class": "", "reason": None}
_GROK_401 = {"exit": 12, "class": "DEV_AUTH", "reason": "terminal_error"}
_GROK_EMPTY = {
    "exit": 15, "class": "DEV_HARNESS_FAULT", "reason": "terminal_error",
}

# Claude 401: no committed capture — CLI has never been stub-driven in a
# failure scenario (PLAN_CLI_PINS §5). Visible decision, not a silent skip.
_CLAUDE_401_SKIP = (
    "no committed claude_http_401 capture; Claude CLI has never been "
    "stub-driven in a failure scenario"
)

_MATRIX: dict[str, tuple[RowSpec, ...]] = {
    "grok-build": (
        RowSpec("healthy", required=True, expected=_NO_FAULT),
        RowSpec("http_401", required=True, expected=_GROK_401),
        RowSpec("empty", required=True, expected=_GROK_EMPTY),
        RowSpec("plan_mode", required=True, expected=None, kind="flag_accept"),
        RowSpec("resume", required=True, expected=_NO_FAULT),
    ),
    "claude-code": (
        RowSpec("healthy", required=True, expected=_NO_FAULT),
        RowSpec("http_401", required=False, expected=None,
                skip_reason=_CLAUDE_401_SKIP),
        RowSpec("empty", required=True, expected={
            "exit": 15, "class": "DEV_HARNESS_FAULT",
            "reason": "empty_completion",
        }),
        RowSpec("plan_mode", required=True, expected=None, kind="flag_accept"),
        RowSpec("resume", required=True, expected=_NO_FAULT),
    ),
    "codex": (
        RowSpec("healthy", required=True, expected=_NO_FAULT),
        RowSpec("http_401", required=True, expected={
            "exit": 12, "class": "DEV_AUTH", "reason": "terminal_error",
        }),
        RowSpec("empty", required=True, expected={
            "exit": 15, "class": "DEV_HARNESS_FAULT",
            "reason": "empty_completion",
        }),
        RowSpec("plan_mode", required=True, expected=None, kind="flag_accept"),
        RowSpec("resume", required=True, expected=_NO_FAULT),
    ),
}


def matrix_for(template: str) -> tuple[RowSpec, ...]:
    try:
        return _MATRIX[template]
    except KeyError:
        raise ValueError(f"no probe matrix for template {template!r}") from None
