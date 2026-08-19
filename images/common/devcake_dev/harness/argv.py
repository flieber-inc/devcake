"""Harness argv construction (docs/08 §1).

Identity lives on HarnessDialect (docs/16 H1). This module is the stable
import path for the capture rig and the entrypoint: one function, same
argv production uses.
"""
from __future__ import annotations

import subprocess

from .dialect import ResumeSpec, WORKSPACE, dialects, get_dialect


def resume_specs() -> dict[str, ResumeSpec]:
    """Capture-verified resume facts, derived from each dialect's resume_spec."""
    return {d.id: d.resume_spec for d in dialects().values()
            if d.resume_spec is not None}


RESUME_SPECS: dict[str, ResumeSpec] = resume_specs()


def cli_version(harness: str, *, timeout: float = 5.0) -> str:
    """First line of `<cli> --version` in this container. Empty on unknown
    harness, missing binary, or timeout — never raises into provision."""
    if not harness:
        return ""
    try:
        exe = get_dialect(harness).argv(".")[0]
    except ValueError:
        return ""
    try:
        r = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    line = (r.stdout or r.stderr).strip().splitlines()
    return line[0][:120] if line else ""


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


