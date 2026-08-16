"""Harness argv construction (docs/08 §1).

Identity lives on HarnessDialect (docs/16 H1). This module is the stable
import path for the capture rig and the entrypoint: one function, same
argv production uses.
"""
from __future__ import annotations

from .dialect import ResumeSpec, WORKSPACE, dialects, get_dialect


def resume_specs() -> dict[str, ResumeSpec]:
    """Capture-verified resume facts, derived from each dialect's resume_spec."""
    return {d.id: d.resume_spec for d in dialects().values()
            if d.resume_spec is not None}


RESUME_SPECS: dict[str, ResumeSpec] = resume_specs()


def forge_dialect(env: dict) -> tuple:
    """(clone_user, git_name, git_email, cli_token_envs) for the clone
    bootstrap. Values come from the app's ForgeDescriptor via spec_env
    (docs/06, docs/07). App and images deploy in lockstep (docs/13 §8), so
    every var is always present — a KeyError here means a mismatched build
    and should crash the run loudly."""
    cli_envs = [e for e in env.get("DEVCAKE_FORGE_CLI_ENVS", "").split(",") if e]
    return (env["DEVCAKE_CLONE_USER"], env["DEVCAKE_GIT_NAME"],
            env["DEVCAKE_GIT_EMAIL"], cli_envs)


def harness_argv(harness: str, prompt: str, *, plan_mode: bool = False,
                 model: str = "", extra=(), out_dir=None) -> list:
    """The harness command line (docs/08 §1) — ONE definition.

    Extracted from `main()` so the capture rig (`scripts/harness_capture/`)
    builds argv through the SAME code path production uses. A fixture captured
    with even slightly different flags silently stops corresponding to what the
    predicate sees on a real run, and that divergence is invisible in review —
    the stream still looks plausible.

    `out_dir` defaults to /workspace/out (codex's `-o` target); the capture rig
    points it at a throwaway directory.
    """
    extra = list(extra)
    return get_dialect(harness).argv(
        prompt, plan_mode=plan_mode, model=model, extra=extra, out_dir=out_dir)


def harness_resume_argv(harness: str, session_id: str, prompt: str, *,
                        model: str = "", extra=(), out_dir=None) -> list | None:
    """CANDIDATE session-resume command lines (ADR-0022), or None for a
    harness with no candidate. Each branch keeps the same output format and
    approval flags as its harness_argv arm so the stream parsers and the
    fault predicate see the identical shape; extras come last so they can
    override, same contract as harness_argv.

    Production (the continuation loop) calls this only for harnesses present
    in RESUME_SPECS; the capture rig calls it directly to TEST a candidate —
    same builder both times, so a fixture cannot drift from the invocation
    production would use. No plan_mode parameter: plan runs never continue.
    Unknown ids raise (same contract as harness_argv / get_dialect). None
    means a known dialect has no resume candidate."""
    extra = list(extra)
    return get_dialect(harness).resume_argv(
        session_id, prompt, model=model, extra=extra, out_dir=out_dir)


