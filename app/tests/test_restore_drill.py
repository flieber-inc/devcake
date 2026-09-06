"""The destructive CI drill must refuse local and self-hosted execution."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("actions,runner", [(None, None), ("false", "github-hosted"),
                                           ("true", "self-hosted"), ("true", None)])
def test_restore_drill_refuses_non_disposable_runner(tmp_path, actions, runner):
    script = Path("/srv/repo-scripts/ci_backup_restore.sh")
    if not script.parent.exists():
        script = Path(__file__).resolve().parents[2] / "scripts/ci_backup_restore.sh"
    touched = tmp_path / "docker-called"
    docker = tmp_path / "docker"
    docker.write_text(f"#!/bin/sh\ntouch '{touched}'\nexit 0\n")
    docker.chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if k not in {"GITHUB_ACTIONS", "RUNNER_ENVIRONMENT"}}
    env["PATH"] = str(tmp_path) + os.pathsep + env["PATH"]
    if actions is not None:
        env["GITHUB_ACTIONS"] = actions
    if runner is not None:
        env["RUNNER_ENVIRONMENT"] = runner
    result = subprocess.run(["bash", str(script)], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "GitHub-hosted" in result.stderr
    assert not touched.exists()
