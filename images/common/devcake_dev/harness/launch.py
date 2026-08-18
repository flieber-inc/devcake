"""How a Dev starts.

Default: aim() has already written env/files; an operator script is
additive (MCP add, PATH) and then the dialect argv is exec'd.
override=True: dialect argv is not used — the operator script is the process.
session_id set (and not override): resume dialect argv, then the same
additive-prelude wrap as a fresh launch.

Prod entrypoint and the hermetic probe call this one function.
"""

from __future__ import annotations

import shlex

from .dialect import get_dialect


def composed_launch(
    template: str,
    prompt: str,
    *,
    plan_mode: bool = False,
    model: str = "",
    extra: tuple[str, ...] | list[str] = (),
    script: str = "",
    override: bool = False,
    out_dir=None,
    session_id: str | None = None,
) -> list[str]:
    body = (script or "").strip()
    if override:
        if not body:
            raise ValueError(
                "override_harness_adapter is set but the entrypoint "
                "script is empty")
        return ["bash", "--noprofile", "--norc", "-c", body]
    dialect = get_dialect(template)
    if session_id:
        # Plan never continues — no plan_mode on resume (ADR-0022).
        dialect_argv = dialect.resume_argv(
            session_id, prompt, model=model,
            extra=list(extra), out_dir=out_dir)
        if dialect_argv is None:
            raise ValueError(
                f"harness {template!r} has no resume dialect")
    else:
        dialect_argv = dialect.argv(
            prompt, plan_mode=plan_mode, model=model,
            extra=list(extra), out_dir=out_dir)
    if not body:
        return dialect_argv
    quoted = " ".join(shlex.quote(p) for p in dialect_argv)
    return ["bash", "--noprofile", "--norc", "-c",
            f"{body}\nexec {quoted}\n"]
