"""Harness argv construction (docs/08 §1)."""
from __future__ import annotations

import pathlib

WORKSPACE = pathlib.Path("/workspace")

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


