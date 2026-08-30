"""``devcake doctor`` — named preflight catalog (ADR-0038 Decision 1).

Never runs sudo / usermod / loginctl enable-linger. Exit 3 when a hard check
fails (steady-state would not work). Soft / platform-skip checks stay ok.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .paths import find_checkout_root

# Stable ids once shipped (ADR-0038 / CAKE-177 plan). Order is intentional.
CHECK_IDS: tuple[str, ...] = (
    "docker_socket",
    "docker_group",
    "docker_gid",
    "buildx",
    "checkout_layout",
    "digest_lockstep",
    "user_session_linger",
    "ports",
    "baker_liveness",
)

# Host ports the control plane publishes on loopback (docs/13).
_CONTROL_PORTS: tuple[tuple[int, str], ...] = (
    (8080, "admin"),
    (8525, "dagu UI"),
    (5080, "OpenObserve"),
    (3300, "Gitea"),
)


@dataclass(frozen=True)
class CheckResult:
    id: str
    ok: bool
    detail: str
    hard: bool = True  # hard failure → exit 3 when ok is False


def _sock_path() -> Path:
    return Path(os.environ.get("DOCKER_SOCK", "/var/run/docker.sock"))


def check_docker_socket(*, sock: Path | None = None) -> CheckResult:
    path = sock or _sock_path()
    if not path.exists():
        return CheckResult(
            id="docker_socket",
            ok=False,
            detail=(
                f"Docker socket not found at {path}. "
                f"Start the Docker daemon (or Docker Desktop), or set DOCKER_SOCK "
                f"to the socket path."
            ),
        )
    if not os.access(path, os.R_OK):
        return CheckResult(
            id="docker_socket",
            ok=False,
            detail=(
                f"Docker socket {path} exists but is not readable by this user. "
                f"Fix permissions or join the docker group (see docker_group check)."
            ),
        )
    return CheckResult(
        id="docker_socket",
        ok=True,
        detail=f"socket readable at {path}",
    )


def check_docker_group(*, sock: Path | None = None) -> CheckResult:
    """Linux: user should be in the docker group when the sock is group-owned.

    macOS / Docker Desktop typically uses a different access model — report ok
    with an explanatory detail when not Linux.
    """
    if platform.system() != "Linux":
        return CheckResult(
            id="docker_group",
            ok=True,
            detail=f"skipped on {platform.system()} (no Linux docker group)",
            hard=False,
        )
    path = sock or _sock_path()
    try:
        import grp

        st = path.stat() if path.exists() else None
        if st is None:
            return CheckResult(
                id="docker_group",
                ok=False,
                detail=(
                    "cannot verify docker group membership — socket missing "
                    f"({path}). Start Docker first."
                ),
            )
        try:
            group = grp.getgrgid(st.st_gid)
            gname = group.gr_name
        except KeyError:
            gname = str(st.st_gid)
        # Root-owned socket (gid 0) is common on Desktop / rootful engines —
        # group membership is not the remedy then.
        if st.st_gid == 0:
            return CheckResult(
                id="docker_group",
                ok=True,
                detail="socket gid is 0 (root group); docker group N/A",
                hard=False,
            )
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        try:
            import pwd

            user = user or pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            pass
        try:
            members = set(grp.getgrnam(gname).gr_mem)
            # primary group also counts
            if os.getgid() == st.st_gid or user in members:
                return CheckResult(
                    id="docker_group",
                    ok=True,
                    detail=f"user {user!r} is in group {gname!r}",
                )
        except KeyError:
            pass
        return CheckResult(
            id="docker_group",
            ok=False,
            detail=(
                f"user {user!r} is not in group {gname!r} (socket gid {st.st_gid}). "
                f"One-time fix (printed only; this CLI will not run it): "
                f"sudo usermod -aG {gname} {user} && newgrp {gname}"
            ),
        )
    except OSError as exc:
        return CheckResult(
            id="docker_group",
            ok=False,
            detail=f"could not inspect socket group: {exc}",
        )


def check_docker_gid(
    *,
    repo_root: Path | None,
    sock: Path | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> CheckResult:
    """DOCKER_GID must be derivable via scripts/lib/stack_env.sh chokepoint."""
    path = sock or _sock_path()
    if repo_root is None or not (repo_root / "scripts" / "lib" / "stack_env.sh").is_file():
        return CheckResult(
            id="docker_gid",
            ok=False,
            detail=(
                "cannot derive DOCKER_GID — checkout scripts/lib/stack_env.sh missing. "
                "Run from the DevCake repo root."
            ),
        )
    if not path.exists():
        return CheckResult(
            id="docker_gid",
            ok=False,
            detail=(
                f"cannot derive DOCKER_GID — socket {path} missing. "
                f"Start Docker / Docker Desktop, or set DOCKER_SOCK."
            ),
        )
    runner = run or subprocess.run
    helper = repo_root / "scripts" / "lib" / "stack_env.sh"
    script = (
        f"set -euo pipefail\n"
        f"source {helper.as_posix()!r}\n"
        f"devcake_docker_gid {path.as_posix()!r}\n"
    )
    try:
        proc = runner(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            id="docker_gid",
            ok=False,
            detail=f"DOCKER_GID probe failed: {exc}",
        )
    gid = (proc.stdout or "").strip()
    if proc.returncode != 0 or not gid.isdigit():
        return CheckResult(
            id="docker_gid",
            ok=False,
            detail=(
                f"cannot derive DOCKER_GID from {path} "
                f"(rc={proc.returncode}). Is the Docker daemon running? "
                f"On Docker Desktop, ensure the engine is up; override via "
                f"DOCKER_GID in .env only after confirming the in-container view."
            ),
        )
    return CheckResult(
        id="docker_gid",
        ok=True,
        detail=f"DOCKER_GID={gid} derivable from {path}",
    )


def check_buildx(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> CheckResult:
    runner = run or subprocess.run
    docker = shutil.which("docker")
    if not docker:
        return CheckResult(
            id="buildx",
            ok=False,
            detail="docker not on PATH — install Docker Engine + Buildx (or Docker Desktop).",
        )
    try:
        proc = runner(
            [docker, "buildx", "version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            id="buildx",
            ok=False,
            detail=f"docker buildx probe failed: {exc}",
        )
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    # Real Buildx accepts `bake -f <file>`; buildah's shim rejects `-f` as an
    # unknown shorthand (and often prints "buildah" from `buildx version`).
    try:
        bake = runner(
            [docker, "buildx", "bake", "-f", "/dev/null"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            id="buildx",
            ok=False,
            detail=f"docker buildx bake probe failed: {exc}",
        )
    bake_out = ((bake.stdout or "") + (bake.stderr or "")).lower()
    if "unknown shorthand flag" in bake_out or "unknown flag: 'f'" in bake_out:
        return CheckResult(
            id="buildx",
            ok=False,
            detail=(
                "docker buildx bake is not available (buildah/podman shim detected). "
                "Install Docker Engine + Buildx (or Docker Desktop). "
                f"version: {out.splitlines()[0] if out else 'unknown'}"
            ),
        )
    # Any other response (missing file, HCL parse error, help) means the bake
    # subcommand exists — good enough for preflight.
    if bake.returncode == 0 or "bake" in bake_out or "hcl" in bake_out or "open" in bake_out:
        return CheckResult(
            id="buildx",
            ok=True,
            detail=f"docker buildx bake available ({out.splitlines()[0] if out else 'ok'})",
        )
    return CheckResult(
        id="buildx",
        ok=False,
        detail=(
            "docker buildx bake is not available. Install/enable Docker Buildx "
            "(Engine + buildx plugin), or use Docker Desktop. "
            f"Probe output: {out[:200] or f'rc={proc.returncode}'}"
        ),
    )


def check_checkout_layout(*, repo_root: Path | None) -> CheckResult:
    if repo_root is None:
        return CheckResult(
            id="checkout_layout",
            ok=False,
            detail=(
                "not a DevCake checkout (need docker-compose.yml + docker-bake.hcl). "
                "cd to the repo root or re-clone."
            ),
        )
    missing: list[str] = []
    for rel in (
        "docker-compose.yml",
        "docker-bake.hcl",
        "scripts/dev_factory",
        "scripts/lib/stack_env.sh",
        "scripts/lib/baker_host.sh",
    ):
        p = repo_root / rel
        if not (p.is_file() or p.is_dir()):
            missing.append(rel)
    if missing:
        return CheckResult(
            id="checkout_layout",
            ok=False,
            detail=(
                f"checkout incomplete under {repo_root}: missing {', '.join(missing)}. "
                f"Re-clone or run from the DevCake repo root."
            ),
        )
    return CheckResult(
        id="checkout_layout",
        ok=True,
        detail=f"checkout layout ok at {repo_root}",
    )


def check_digest_lockstep(*, repo_root: Path | None) -> CheckResult:
    if repo_root is None:
        return CheckResult(
            id="digest_lockstep",
            ok=False,
            detail="no checkout — cannot check app digest tooling",
        )
    digest_py = repo_root / "scripts" / "app_digest.py"
    if not digest_py.is_file():
        return CheckResult(
            id="digest_lockstep",
            ok=False,
            detail=(
                "scripts/app_digest.py missing — digest-stamped bake cannot run. "
                "Re-clone the repo."
            ),
        )
    # Without a live stack we only prove the tooling exists; next step is bake.
    return CheckResult(
        id="digest_lockstep",
        ok=True,
        detail=(
            "app digest tooling present (scripts/app_digest.py). "
            "For a lockstep bake+compose pin run: devcake up --bake"
        ),
        hard=False,
    )


def check_user_session_linger(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> CheckResult:
    """Linux systemd --user linger; skipped on macOS / non-systemd hosts."""
    system = platform.system()
    if system == "Darwin":
        return CheckResult(
            id="user_session_linger",
            ok=True,
            detail="skipped on macOS (launchd; no linger)",
            hard=False,
        )
    if system != "Linux":
        return CheckResult(
            id="user_session_linger",
            ok=True,
            detail=f"skipped on {system}",
            hard=False,
        )
    runner = run or subprocess.run
    if not shutil.which("systemctl"):
        return CheckResult(
            id="user_session_linger",
            ok=True,
            detail="systemd not present — baker will use flock respawn (DEGRADED)",
            hard=False,
        )
    # Probe user bus; missing session → linger remedy (printed only).
    try:
        probe = runner(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            id="user_session_linger",
            ok=False,
            hard=False,
            detail=(
                f"systemd --user probe failed ({exc}). "
                f"One-time fix (printed only): loginctl enable-linger "
                f"{os.environ.get('USER', '$USER')} then re-login, then "
                f"devcake up"
            ),
        )
    if probe.returncode == 0:
        return CheckResult(
            id="user_session_linger",
            ok=True,
            detail="systemd --user session available",
            hard=False,
        )
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "$USER"
    # Soft: up still works via flock respawn, but name the native path.
    return CheckResult(
        id="user_session_linger",
        ok=False,
        hard=False,
        detail=(
            "systemd --user session missing (baker would fall back to DEGRADED "
            "flock respawn). One-time fix (printed only; this CLI will not run it): "
            f"loginctl enable-linger {user} && re-login (or reboot), then "
            f"devcake up"
        ),
    )


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def check_ports(
    *,
    ports: Sequence[tuple[int, str]] | None = None,
    probe: Callable[[int], bool] | None = None,
) -> CheckResult:
    """Warn when documented control-plane ports are already occupied.

    Occupied ports are soft failures: an already-running DevCake stack is a
    valid state; a foreign listener needs operator attention before a fresh up.
    """
    probe_fn = probe or _port_in_use
    conflicts: list[str] = []
    for port, label in ports or _CONTROL_PORTS:
        if probe_fn(port):
            conflicts.append(f"{port} ({label})")
    if conflicts:
        return CheckResult(
            id="ports",
            ok=False,
            hard=False,
            detail=(
                "host ports already in use: "
                + ", ".join(conflicts)
                + ". If this is an existing DevCake stack, ok — use "
                "devcake status. If another process holds them, free the port "
                "or change the published bind in compose override."
            ),
        )
    return CheckResult(
        id="ports",
        ok=True,
        detail="documented control-plane ports appear free on 127.0.0.1",
        hard=False,
    )


def check_baker_liveness(*, repo_root: Path | None) -> CheckResult:
    """When .factory implies a baker, check pidfile liveness; else honest skip."""
    if repo_root is None:
        return CheckResult(
            id="baker_liveness",
            ok=True,
            hard=False,
            detail="no checkout — baker check skipped",
        )
    factory = repo_root / ".factory"
    pidfile = factory / "watch.pid"
    if not factory.is_dir() and not pidfile.is_file():
        return CheckResult(
            id="baker_liveness",
            ok=True,
            hard=False,
            detail="no .factory yet — baker not expected; run: devcake up",
        )
    if not pidfile.is_file():
        return CheckResult(
            id="baker_liveness",
            ok=False,
            hard=False,
            detail=(
                f"{factory} exists but watch.pid is missing — baker not running. "
                f"Fix: devcake up"
            ),
        )
    raw = pidfile.read_text(encoding="utf-8", errors="replace").strip()
    try:
        pid = int(raw.splitlines()[0].strip())
    except (ValueError, IndexError):
        return CheckResult(
            id="baker_liveness",
            ok=False,
            hard=False,
            detail=f"invalid pidfile {pidfile}: {raw!r}. Fix: devcake up",
        )
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return CheckResult(
            id="baker_liveness",
            ok=False,
            hard=False,
            detail=(
                f"baker pidfile names pid {pid} but process is dead. "
                f"Fix: devcake up"
            ),
        )
    except PermissionError:
        # Process exists but we cannot signal it — treat as alive-ish.
        return CheckResult(
            id="baker_liveness",
            ok=True,
            hard=False,
            detail=f"baker pid {pid} exists (signal not permitted)",
        )
    return CheckResult(
        id="baker_liveness",
        ok=True,
        hard=False,
        detail=f"baker pid {pid} is alive",
    )


def run_checks(
    *,
    repo_root: Path | None = None,
    sock: Path | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    port_probe: Callable[[int], bool] | None = None,
) -> list[CheckResult]:
    root = repo_root if repo_root is not None else find_checkout_root()
    sock_path = sock or _sock_path()
    return [
        check_docker_socket(sock=sock_path),
        check_docker_group(sock=sock_path),
        check_docker_gid(repo_root=root, sock=sock_path, run=run),
        check_buildx(run=run),
        check_checkout_layout(repo_root=root),
        check_digest_lockstep(repo_root=root),
        check_user_session_linger(run=run),
        check_ports(probe=port_probe),
        check_baker_liveness(repo_root=root),
    ]


def _format_human(checks: Sequence[CheckResult]) -> str:
    lines: list[str] = ["devcake doctor"]
    for c in checks:
        mark = "ok" if c.ok else "FAIL"
        lines.append(f"  [{mark}] {c.id}: {c.detail}")
    hard_fails = [c for c in checks if (not c.ok) and c.hard]
    soft_fails = [c for c in checks if (not c.ok) and not c.hard]
    if hard_fails:
        lines.append(
            f"preflight failed ({len(hard_fails)} hard) — steady-state would not work"
        )
    elif soft_fails:
        lines.append(
            f"warnings ({len(soft_fails)} soft) — see remedies above; "
            f"hard preflight ok"
        )
    else:
        lines.append("all checks ok")
    return "\n".join(lines) + "\n"


def run_doctor(*, as_json: bool = False, repo_root: Path | None = None) -> int:
    """Execute the catalog. Returns 0 or 3 (ADR-0038 exit table)."""
    checks = run_checks(repo_root=repo_root)
    hard_fail = any((not c.ok) and c.hard for c in checks)
    ok = not hard_fail
    if as_json:
        payload = {
            "ok": ok,
            "schema_version": 1,
            "checks": [
                {"id": c.id, "ok": c.ok, "detail": c.detail} for c in checks
            ],
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stdout.write(_format_human(checks))
    return 0 if ok else 3
