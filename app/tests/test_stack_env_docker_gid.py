"""CAKE-128: stack_env.sh DOCKER_GID helpers (host + in-container).

Public seams: ``devcake_docker_gid`` and ``devcake_docker_gid_incontainer``.
Policy (fail-hard vs permissive) stays with callers — these tests only cover
derivation contracts. Stub ``docker`` on PATH; do not hit a real daemon.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

# Local checkout: …/devcake/scripts/lib; app-test: /srv/repo-scripts/lib.
_STACK_ENV_CANDIDATES = (
    Path(__file__).resolve().parents[2] / "scripts" / "lib" / "stack_env.sh",
    Path("/srv/repo-scripts/lib/stack_env.sh"),
)


def _stack_env() -> Path:
    path = next((p for p in _STACK_ENV_CANDIDATES if p.is_file()), None)
    assert path is not None, (
        "scripts/lib/stack_env.sh missing — bind scripts → /srv/repo-scripts"
    )
    return path


def _run_helper(
    tmp_path: Path,
    *,
    fn: str,
    sock: Path,
    docker_script: str | None,
) -> subprocess.CompletedProcess[str]:
    """Source stack_env.sh and invoke ``fn`` with a stubbed ``docker`` on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if docker_script is not None:
        docker = bin_dir / "docker"
        docker.write_text(docker_script)
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    # Isolate from a real docker earlier on PATH when we intentionally omit the stub.
    if docker_script is None:
        # Empty bin_dir first on PATH; no docker executable → command not found.
        pass
    script = f"""
set -euo pipefail
source {_stack_env().as_posix()!r}
{fn} {sock.as_posix()!r}
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )


def test_incontainer_probe_success_returns_container_view_gid(tmp_path: Path):
    sock = tmp_path / "docker.sock"
    sock.write_text("")  # existence only; bind path is passed through to stub
    # Stub records argv and prints the in-container gid the mission requires.
    stub = tmp_path / "bin"
    stub.mkdir()
    log = tmp_path / "docker-argv.log"
    docker = stub / "docker"
    docker.write_text(
        f"""#!/bin/sh
printf '%s\\n' "$*" >> {log.as_posix()!r}
# Only the probe's `docker run` path must succeed with a gid.
case " $* " in
  *" run "*) echo 0; exit 0 ;;
esac
echo "unexpected docker invocation: $*" >&2
exit 99
"""
    )
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)

    env = {**os.environ, "PATH": f"{stub}{os.pathsep}{os.environ.get('PATH', '')}"}
    script = f"""
set -euo pipefail
source {_stack_env().as_posix()!r}
devcake_docker_gid_incontainer {sock.as_posix()!r}
"""
    got = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env,
    )
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "0"
    argv = log.read_text()
    assert f"-v {sock}:/var/run/docker.sock:ro" in argv or (
        f"-v{sock}:/var/run/docker.sock:ro" in argv
    )
    assert "stat -c %g /var/run/docker.sock" in argv or (
        "stat" in argv and "%g" in argv and "/var/run/docker.sock" in argv
    )


def test_incontainer_probe_failure_returns_rc1_and_empty_stdout(tmp_path: Path):
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    stub = tmp_path / "bin"
    stub.mkdir()
    docker = stub / "docker"
    docker.write_text("#!/bin/sh\nexit 1\n")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)

    env = {**os.environ, "PATH": f"{stub}{os.pathsep}{os.environ.get('PATH', '')}"}
    script = f"""
set +e
source {_stack_env().as_posix()!r}
out="$(devcake_docker_gid_incontainer {sock.as_posix()!r})"
rc=$?
printf 'RC=%s\\n' "$rc"
printf 'OUT=%s\\n' "$out"
"""
    got = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env,
    )
    assert got.returncode == 0, got.stderr  # wrapper itself succeeds
    assert "RC=1" in got.stdout
    assert "OUT=\n" in got.stdout or got.stdout.rstrip().endswith("OUT=")


def test_host_docker_gid_returns_numeric_gid_for_statable_path(tmp_path: Path):
    sock = tmp_path / "fake.sock"
    sock.write_text("")
    expected = subprocess.run(
        ["stat", "-c", "%g", str(sock)], capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert expected.isdigit()

    got = _run_helper(
        tmp_path, fn="devcake_docker_gid", sock=sock, docker_script=None,
    )
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == expected
