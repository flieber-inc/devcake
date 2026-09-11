"""``devcake doctor`` — named preflight catalog (ADR-0038 Decision 1).

Never runs sudo / usermod / loginctl enable-linger. Exit 3 when a hard check
fails (steady-state would not work). Soft / platform-skip checks stay ok.
"""

from __future__ import annotations

import hashlib
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

from . import envfile
from .paths import find_checkout_root, read_version_pin

# Stable ids once shipped (ADR-0038 / CAKE-177 plan). Order is intentional.
CHECK_IDS: tuple[str, ...] = (
    "docker_socket",
    "docker_group",
    "docker_gid",
    "buildx",
    "checkout_layout",
    "digest_lockstep",
    "version_pin",
    "user_session_linger",
    "ports",
    "baker_liveness",
    "apparmor_profile",
)

# The AppArmor profile dev-run.yaml names on both Dev steps (ADR-0023
# addendum): the checkout ships it, the operator loads it, `devcake up`
# writes the resulting name into .env. One detection helper serves both
# the doctor's check and up's derivation — never derived twice.
APPARMOR_PROFILE_NAME = "devcake-nested"
APPARMOR_PROFILE_REL = Path("scripts") / "apparmor" / APPARMOR_PROFILE_NAME
APPARMOR_INSTALLED_PATH = Path("/etc/apparmor.d") / APPARMOR_PROFILE_NAME
_APPARMOR_ENABLED_PATH = Path("/sys/module/apparmor/parameters/enabled")
_APPARMOR_PROFILES_PATH = Path("/sys/kernel/security/apparmor/profiles")
DOCKER_DEFAULT_PROFILE = "docker-default"

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


def check_version_pin(*, repo_root: Path | None) -> CheckResult:
    """The checkout's release pin (`VERSION`) against the tag the stack was
    brought up under (`DEVCAKE_TAG` in `.env`, written by `devcake up`). A
    drift means the running images are not the checkout's release; the
    remedy is one command. Soft: a scratch build (a process-env override)
    drifts on purpose."""
    if repo_root is None:
        return CheckResult(id="version_pin", ok=False, hard=False,
                           detail="no checkout — cannot read VERSION")
    pinned = read_version_pin(repo_root)
    if not pinned:
        return CheckResult(
            id="version_pin", ok=False, hard=False,
            detail=("VERSION missing at the checkout root — the image tag falls "
                    "back to latest. A release checkout carries its own pin "
                    "(CONTRIBUTING.md, Cutting a release)."))
    env_path = repo_root / ".env"
    running = ""
    if env_path.is_file():
        running = (envfile.parse_env_file(env_path).get("DEVCAKE_TAG") or "").strip()
    if not running:
        return CheckResult(
            id="version_pin", ok=True, hard=False,
            detail=f"checkout pins {pinned}; no stack brought up yet "
                   f"(.env carries no DEVCAKE_TAG) — devcake up --release")
    if running != pinned:
        override = os.environ.get("DEVCAKE_TAG", "").strip()
        why = (" (a DEVCAKE_TAG override is set in this shell)"
               if override == running else "")
        return CheckResult(
            id="version_pin", ok=False, hard=False,
            detail=(f"checkout pins {pinned} but the stack was brought up under "
                    f"{running}{why} — run: devcake up --release"))
    return CheckResult(id="version_pin", ok=True,
                       detail=f"checkout pins {pinned} and the stack runs under it")


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


@dataclass(frozen=True)
class ApparmorFacts:
    """What this host says about the Dev-container profile. Evidence in
    order of authority: `applies` (the daemon accepted the name on a
    throwaway container — definitive), `loaded` (the kernel's profile list;
    None when unreadable without root, the stock-Ubuntu case), then the file
    under /etc/apparmor.d/ plus whether the host's own parser compiles the
    checkout's copy (an older parser rejects the `userns` rule: the install
    succeeds, the load fails)."""
    enabled: bool                 # the DAEMON runs with AppArmor (docker info)
    loaded: bool | None
    installed: bool
    current: bool | None          # installed file == the checkout's; None when unknown
    parser: bool                  # apparmor_parser found
    compiles: bool | None         # host parser compiles the checkout's file; None = no parser
    applies: bool | None          # docker run --security-opt apparmor=<name> succeeded; None = not tried

    @property
    def usable(self) -> bool:
        """The name may be handed to Docker."""
        if not self.enabled:
            return False
        if self.applies is not None:
            return self.applies
        if self.loaded is True:
            return True
        if self.loaded is None and self.installed:
            return self.compiles is not False
        return False


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _daemon_has_apparmor(runner, docker: str | None) -> bool | None:
    """From `docker info` — the daemon's host is what matters (Docker
    Desktop's VM and remote contexts differ from the CLI's host). None when
    the daemon cannot be asked."""
    if not docker:
        return None
    try:
        proc = runner([docker, "info", "--format", "{{json .SecurityOptions}}"],
                      capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        opts = json.loads((proc.stdout or "").strip() or "null")
    except json.JSONDecodeError:
        return None
    if not isinstance(opts, list):
        return None                      # a daemon that answers null: unknown
    return any(str(o).startswith("name=apparmor") for o in opts)


def _probe_image(runner, docker: str) -> str | None:
    """A local image to start a throwaway container from — hello (this
    stack's own) or the pinned redis; never an arbitrary local image (its
    /bin/true is its own code)."""
    try:
        proc = runner([docker, "images", "--format", "{{.Repository}}:{{.Tag}}"],
                      capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    refs = [r.strip() for r in (proc.stdout or "").splitlines()
            if r.strip() and not r.endswith(":<none>")]
    for pref in ("devcake/dev-hello:", "redis:"):
        for r in refs:
            if r.startswith(pref):
                return r
    return None


def _profile_applies(runner, docker: str, image: str) -> bool | None:
    """Ask the daemon: a container that names the profile either starts
    (True) or fails to apply it (False). Anything else: unknown."""
    try:
        proc = runner([docker, "run", "--rm", "--network", "none",
                       "--user", "65534:65534", "--cap-drop", "ALL",
                       "--security-opt", "no-new-privileges",
                       "--pids-limit", "8", "--memory", "32m", "--read-only",
                       "--security-opt", f"apparmor={APPARMOR_PROFILE_NAME}",
                       "--entrypoint", "/bin/true", image],
                      capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0:
        return True
    if "apparmor" in (proc.stderr or "").lower():
        return False
    return None


def apparmor_facts(
    *,
    repo_root: Path | None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    enabled_path: Path = _APPARMOR_ENABLED_PATH,
    profiles_path: Path = _APPARMOR_PROFILES_PATH,
    installed_path: Path = APPARMOR_INSTALLED_PATH,
    docker: str | None = None,
    parser: str | None = None,
) -> ApparmorFacts:
    runner = run or subprocess.run
    docker_bin = docker or shutil.which("docker")
    enabled = _daemon_has_apparmor(runner, docker_bin)
    if enabled is None:                      # daemon unreachable: the CLI host's kernel
        try:
            enabled = enabled_path.read_text().strip().upper().startswith("Y")
        except OSError:
            enabled = False
    loaded: bool | None = None
    try:
        loaded = any(line.split(" ", 1)[0] == APPARMOR_PROFILE_NAME
                     for line in profiles_path.read_text().splitlines())
    except OSError:
        loaded = None
    installed = installed_path.is_file()
    ours = (repo_root / APPARMOR_PROFILE_REL) if repo_root is not None else None
    current: bool | None = None
    if installed and ours is not None:
        a, b = _sha256(ours), _sha256(installed_path)
        if a and b:
            current = a == b
    parser_bin = (parser or shutil.which("apparmor_parser")
                  or shutil.which("apparmor_parser", path="/usr/sbin:/sbin"))
    compiles: bool | None = None
    if parser_bin and ours is not None and ours.is_file() and enabled:
        try:
            proc = runner([parser_bin, "--skip-kernel-load", "--skip-cache", str(ours)],
                          capture_output=True, text=True, timeout=60)
            compiles = proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            compiles = None
    applies: bool | None = None
    if enabled and docker_bin and (installed or loaded):
        image = _probe_image(runner, docker_bin)
        if image:
            applies = _profile_applies(runner, docker_bin, image)
    return ApparmorFacts(enabled=enabled, loaded=loaded, installed=installed,
                         current=current, parser=bool(parser_bin),
                         compiles=compiles, applies=applies)


def apparmor_install_commands(repo_root: Path | None) -> str:
    src = ((repo_root / APPARMOR_PROFILE_REL) if repo_root is not None
           else APPARMOR_PROFILE_REL)
    return (f"sudo install -m 0644 {src} /etc/apparmor.d/ && "
            f"sudo apparmor_parser -r {APPARMOR_INSTALLED_PATH}")


def _env_apparmor_value(repo_root: Path | None) -> str:
    """The value as compose reads it: last assignment, `export ` and quotes
    stripped, an unquoted trailing ` # comment` dropped — the same shape the
    baker's reader applies (scripts/harness_probe/env_value.py)."""
    if repo_root is None:
        return ""
    env_path = repo_root / ".env"
    if not env_path.is_file():
        return ""
    value = ""
    try:
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for raw in lines:
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() != "DEVCAKE_APPARMOR_PROFILE":
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        else:
            v = v.split(" #", 1)[0].rstrip()
        value = v
    return value


def check_apparmor_profile(
    *,
    repo_root: Path | None,
    facts: ApparmorFacts | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> CheckResult:
    """The Dev-container AppArmor profile on hosts whose daemon runs
    AppArmor, and whether `.env` names what the host can actually apply.
    Soft: a run launches either way — without the profile the nested engine
    inside Devs is unavailable and every receipt says so."""
    f = facts if facts is not None else apparmor_facts(repo_root=repo_root, run=run)
    env_value = _env_apparmor_value(repo_root)
    if not f.enabled:
        return CheckResult(
            id="apparmor_profile", ok=True, hard=False,
            detail="no AppArmor on the Docker host — the nested engine in Dev "
                   "containers relies on the seccomp profile alone")
    cmds = apparmor_install_commands(repo_root)
    if not f.usable:
        if f.compiles is False:
            why = ("this host's apparmor_parser rejects the profile (4.0 or newer "
                   "is needed for the userns rule; check `apparmor_parser --version`)")
        elif f.applies is False and f.installed:
            why = ("the daemon cannot apply it — the file is installed but the "
                   "kernel has no such profile (unloaded, or never loaded); the "
                   "second command below loads it")
        elif f.applies is False:
            why = "the daemon cannot apply it — the kernel has no such profile"
        else:
            why = "it is not loaded"
        need = ("" if f.parser else
                "install the apparmor package (apparmor_parser) first, then ")
        stale = (" .env still names it — run devcake up so runs fall back to "
                 "docker-default instead of failing at container create."
                 if env_value == APPARMOR_PROFILE_NAME else "")
        return CheckResult(
            id="apparmor_profile", ok=False, hard=False,
            detail=(f"AppArmor is active but the {APPARMOR_PROFILE_NAME} profile "
                    f"is unusable: {why}. Dev containers run under docker-default "
                    "and their nested engine is unavailable. One-time fix (printed "
                    f"only; this CLI will not run it): {need}{cmds} — then devcake up."
                    f"{stale}"))
    if f.current is False:
        return CheckResult(
            id="apparmor_profile", ok=False, hard=False,
            detail=(f"{APPARMOR_PROFILE_NAME} is usable but the installed file "
                    "differs from this checkout's (outdated, or loaded from "
                    "elsewhere). Re-run (printed only; this CLI will not run it): "
                    f"{cmds}"))
    if env_value and env_value != APPARMOR_PROFILE_NAME:
        return CheckResult(
            id="apparmor_profile", ok=False, hard=False,
            detail=(f"{APPARMOR_PROFILE_NAME} is usable but .env still names "
                    f"{env_value} — run devcake up to switch the Dev containers "
                    "to it"))
    how = ("the daemon applies it" if f.applies else
           "loaded" if f.loaded else
           "installed under /etc/apparmor.d/ and compiled by this host's parser "
           "(loaded state unreadable without root)")
    return CheckResult(
        id="apparmor_profile", ok=True, hard=False,
        detail=f"{APPARMOR_PROFILE_NAME}: {how} — Dev containers get the nested engine")


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
        check_version_pin(repo_root=root),
        check_user_session_linger(run=run),
        check_ports(probe=port_probe),
        check_baker_liveness(repo_root=root),
        check_apparmor_profile(repo_root=root, run=run),
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
