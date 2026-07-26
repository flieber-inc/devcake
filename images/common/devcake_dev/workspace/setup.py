"""Skills install + MCP setup (docs/07 §5)."""
from __future__ import annotations

import base64
import os
import pathlib
import subprocess

def install_skills(skills, home=None, skills_dir=".claude/skills"):
    """Skill-store files from the runspec → $HOME/<skills_dir>/<path> before
    the harness starts. The dir is the harness registry's skills_dir
    (harness.py), delivered as the runspec `skills_dir` key; the default is
    claude-code's dir so an older app that sends no key keeps today's
    behavior. Path-traversal-safe on BOTH the dir and every file path:
    store content is operator-editable, so absolute paths and `..` parts
    are refused. Per-file failures are NON-fatal — skills are additive; the
    notes land in the run log."""
    notes = []
    sd = pathlib.PurePosixPath(skills_dir or ".claude/skills")
    if not sd.parts or sd.is_absolute() or ".." in sd.parts:
        notes.append(f"skills: refused unsafe skills_dir {skills_dir!r} "
                     "— using default")
        sd = pathlib.PurePosixPath(".claude/skills")
    base = pathlib.Path(home or pathlib.Path.home()) / sd
    for sk in skills or []:
        name, wrote = sk.get("name", "?"), 0
        for f in sk.get("files") or []:
            rel = pathlib.PurePosixPath(f.get("path") or "")
            if not rel.parts or rel.is_absolute() or ".." in rel.parts:
                notes.append(f"skill {name}: refused unsafe path "
                             f"{f.get('path')!r}")
                continue
            try:
                target = base / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(base64.b64decode(f.get("content_b64") or "",
                                                    validate=True))
                wrote += 1
            except Exception as e:
                notes.append(f"skill {name}: {rel} failed ({e})")
        notes.append(f"skill {name}: installed {wrote} file(s)")
    return notes

MCP_SETUP_TIMEOUT_SECS = 300

def run_mcp_setup(commands, workdir, timeout=MCP_SETUP_TIMEOUT_SECS):
    """Run the Dev Type's admin-configured MCP setup commands in order.
    Returns (failed_cmd, detail) for the exit-14 artifact, or None when all
    pass. Each command gets a closed stdin, its own process group and a hard
    per-command cap: the heartbeat daemon is already beating when these run,
    so a hung install/interactive prompt would otherwise idle the run to the
    full wall-clock timeout without the watchdog ever firing."""
    import signal
    for cmd in commands:
        proc = subprocess.Popen(cmd, shell=True, cwd=workdir,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # pgid == pid (new session)
            except ProcessLookupError:
                pass
            proc.wait()
            return cmd, f"timed out after {timeout}s"
        if proc.returncode != 0:
            tail = (err or out or "")[-2000:]
            return cmd, f"exit {proc.returncode}: {tail}"
    return None
