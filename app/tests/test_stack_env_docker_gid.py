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


def _compose_yml() -> Path | None:
    for p in (
        Path(__file__).resolve().parents[2] / "docker-compose.yml",
        Path("/srv/docker-compose.yml"),
    ):
        if p.is_file():
            return p
    return None


def _dagu_image_pin() -> str | None:
    """Digest-pinned dagu image from compose — no second pin in the suite."""
    import re

    compose = _compose_yml()
    if compose is None:
        return None
    m = re.search(
        r"image:\s*(ghcr\.io/dagucloud/dagu:[^\s@]+@sha256:[0-9a-f]{64})",
        compose.read_text(),
    )
    return m.group(1) if m else None


def test_dagu_socket_writability_polarity_matches_entrypoint_creds():
    """Wrong DOCKER_GID class must fail ``test -w``; matching gid must pass.

    Mirrors the REVIEW reject evidence on the pinned dagu image: root (bare
    compose exec) false-greens a root-owned 0660 socket; ``--user 1000:1``
    fails; ``--user 1000:0`` passes. Creates the stand-in as root inside the
    container, then re-runs with docker ``--user`` — the same identity switch
    ``devcake up`` uses for the post-start gate. Skips when docker/image unavailable.
    """
    import pytest

    image = _dagu_image_pin()
    if image is None:
        pytest.skip("docker-compose.yml / dagu pin not mounted")

    try:
        inspect = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        pytest.skip("docker CLI not on PATH (hermetic app-test)")

    if inspect.returncode != 0:
        pull = subprocess.run(
            ["docker", "pull", image],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if pull.returncode != 0:
            pytest.skip(
                f"dagu image unavailable for polarity probe: {pull.stderr[-200:]}"
            )

    # Seed a root-owned 0660 file into a tiny volume the follow-up runs share.
    # Mount must stay writable: ``test -w`` fails on :ro binds even for root.
    vol = "devcake-cake128-sock-polarity"
    subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)
    seed = subprocess.run(
        [
            "docker", "run", "--rm", "-v", f"{vol}:/sock",
            "--entrypoint", "sh", image, "-c",
            "install -m 0660 /dev/null /sock/fake.sock && "
            "stat -c '%u:%g %a' /sock/fake.sock",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if seed.returncode != 0:
        pytest.skip(f"could not seed fake socket: {seed.stderr[-200:]}")
    assert "0:0 660" in seed.stdout.replace("\n", " ")

    def _test_w(user: str | None) -> int:
        cmd = ["docker", "run", "--rm", "-v", f"{vol}:/sock", "--entrypoint", "sh"]
        if user is not None:
            cmd.extend(["--user", user])
        cmd.extend([image, "-c", "test -w /sock/fake.sock"])
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
        ).returncode

    try:
        assert _test_w(None) == 0, "root (bare exec) false-greens root-owned 0660"
        assert _test_w("1000:1") != 0, "uid 1000 gid 1 must not write root:root 0660"
        assert _test_w("1000:0") == 0, "uid 1000 gid 0 must write root:root 0660"
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)
