"""How a Dev starts.

Default: aim() has already written env/files; additive operator setup runs
via ``run_mcp_setup`` (docs/07 §5) before this function is called with an
empty script, then the dialect argv (or resume argv when ``session_id`` is
set) is returned for exec.
override=True: dialect argv is not used — the operator script is the
process (fail-closed with ``set -e``). ``session_id`` is ignored under
override — the entrypoint degrades resume→fresh so mode stays truthful
(CAKE-62 honesty).

Prod entrypoint and the hermetic probe call this one function.
"""

from __future__ import annotations

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
        # Fail-closed: a failing line aborts instead of continuing. Hangs are
        # the run wall-clock / watchdog (DEV_TIMEOUT), not the 300 s additive
        # per-command cap — override is the whole process, often a long agent.
        return ["bash", "--noprofile", "--norc", "-c", f"set -e\n{body}\n"]
    if body:
        # Additive setup must not be inlined here: a bash prelude without
        # set -e used to swallow failures and still exec the dialect
        # (CAKE-63). Call run_mcp_setup first, then pass script="".
        raise ValueError(
            "additive entrypoint setup must run via run_mcp_setup; "
            "pass script='' to composed_launch")
    dialect = get_dialect(template)
    if session_id:
        # Plan never continues — no plan_mode on resume (ADR-0022).
        dialect_argv = dialect.resume_argv(
            session_id, prompt, model=model,
            extra=list(extra), out_dir=out_dir)
        if dialect_argv is None:
            raise ValueError(
                f"harness {template!r} has no resume dialect")
        return dialect_argv
    return dialect.argv(
        prompt, plan_mode=plan_mode, model=model,
        extra=list(extra), out_dir=out_dir)


def composed_launch_resume_or_none(
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
) -> list[str] | None:
    """Resume argv, or None when the dialect has no resume arm.

    ``composed_launch`` stays fail-closed for direct callers. The Dev
    entrypoint resume path uses this so a missing resume dialect degrades
    to fresh (``cmd is None``) instead of burning the attempt on ValueError.
    Other ValueErrors (unknown harness, additive refuse, empty override)
    still propagate.
    """
    try:
        return composed_launch(
            template, prompt, plan_mode=plan_mode, model=model,
            extra=extra, script=script, override=override, out_dir=out_dir,
            session_id=session_id)
    except ValueError as e:
        if "no resume dialect" in str(e):
            return None
        raise
