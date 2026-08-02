"""Harness argv construction (docs/08 §1)."""
from __future__ import annotations

import dataclasses
import pathlib

WORKSPACE = pathlib.Path("/workspace")


@dataclasses.dataclass(frozen=True)
class ResumeSpec:
    """Capture-verified resume facts for one harness (ADR-0022 Phase 0).

    usage_cumulative: the resumed terminal event reports usage over the WHOLE
    session, so summing per-invocation reports would double-count — the token
    merge goes last-wins within a resume chain instead."""
    usage_cumulative: bool


# The continuation loop's resume gate: a harness earns its entry ONLY through
# a committed `<harness>_resume_nudge` capture pair (fixtures README rules) —
# absence means fresh-only under `auto`, stop under `resume-only`. The
# candidate argv in harness_resume_argv exists INDEPENDENTLY of this dict so
# the capture rig can exercise a composition before it is verified — the rig
# is the only road into this table. usage_cumulative is a PER-HARNESS fact,
# measured, not assumed: codex reports session-cumulative usage on a resumed
# turn.completed, grok and claude report per-invocation (the capture pairs).
RESUME_SPECS: dict[str, ResumeSpec] = {
    "grok-build": ResumeSpec(usage_cumulative=False),    # grok_resume_nudge_*
    "claude-code": ResumeSpec(usage_cumulative=False),   # claude_resume_nudge_*
    "codex": ResumeSpec(usage_cumulative=True),          # codex_resume_nudge_*
}

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
    out = pathlib.Path(out_dir) if out_dir is not None else WORKSPACE / "out"
    if harness == "grok-build":
        mode = ["--permission-mode", "plan"] if plan_mode else ["--always-approve"]
        pin = ["--model", model] if model else []
        return ["grok", "-p", prompt, "--output-format", "streaming-json",
                *mode, *pin, *extra]
    if harness == "codex":
        mode = (["--sandbox", "read-only"] if plan_mode
                else ["--dangerously-bypass-approvals-and-sandbox"])
        pin = ["-m", model] if model else []
        return ["codex", "exec", prompt, "--json",
                "-o", str(out / "last_message.txt"),
                "--skip-git-repo-check", *mode, *pin, *extra]
    mode = (["--permission-mode", "plan"] if plan_mode
            else ["--dangerously-skip-permissions"])
    pin = ["--model", model] if model else []
    # --verbose is REQUIRED with -p + stream-json (the CLI errors out without it)
    return ["claude", "-p", prompt, "--output-format", "stream-json",
            "--verbose", *mode, *pin, *extra]


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
    There is deliberately no default-harness fallback (unlike harness_argv):
    an unverified resume flag on the wrong CLI is a crash, not a default."""
    extra = list(extra)
    out = pathlib.Path(out_dir) if out_dir is not None else WORKSPACE / "out"
    pin_dashed = ["--model", model] if model else []
    if harness == "grok-build":
        return ["grok", "-p", prompt, "-r", session_id,
                "--output-format", "streaming-json", "--always-approve",
                *pin_dashed, *extra]
    if harness == "codex":
        pin = ["-m", model] if model else []
        return ["codex", "exec", "resume", session_id, prompt, "--json",
                "-o", str(out / "last_message.txt"), "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox", *pin, *extra]
    if harness == "claude-code":
        return ["claude", "-p", prompt, "--resume", session_id,
                "--output-format", "stream-json", "--verbose",
                "--dangerously-skip-permissions", *pin_dashed, *extra]
    return None


