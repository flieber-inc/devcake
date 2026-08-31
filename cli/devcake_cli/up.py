"""``devcake up`` — the stack bring-up verb (ADR-0038 Decision 1)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from . import envfile
from .paths import require_checkout_root


@dataclass
class UpOptions:
    bake: bool = False
    bake_targets: list[str] = field(default_factory=list)
    dry_run: bool = False
    foreground_baker: bool = False
    no_hello_smoke: bool = False
    compose_services: list[str] = field(default_factory=list)
    as_json: bool = False


@dataclass
class UpPlan:
    docker_gid: str
    ws_host: str
    tag: str
    sock: str
    bake: bool
    bake_targets: list[str]
    compose_services: list[str]
    foreground_baker: bool
    no_hello_smoke: bool
    env_seeded: bool
    env_generated: list[str]


def discover_docker_gid(repo: Path, sock: str) -> tuple[str, str]:
    """Return (gid, human_resolution_line). Raises RuntimeError on failure."""
    script = f"""
set -euo pipefail
source "{repo / "scripts/lib/stack_env.sh"}"
SOCK={sock!r}
host_gid=""
in_gid=""
if ! host_gid="$(devcake_docker_gid "$SOCK")"; then
  echo "error: cannot derive DOCKER_GID from $SOCK — is the Docker daemon running?" >&2
  exit 1
fi
if in_gid="$(devcake_docker_gid_incontainer "$SOCK")"; then
  GID="$in_gid"
  if [[ "$in_gid" != "$host_gid" ]]; then
    LINE="── DOCKER_GID=${{GID}}  (in-container view; host path says ${{host_gid}})"
  else
    LINE="── DOCKER_GID=${{GID}}  (from ${{SOCK}})"
  fi
else
  GID="$host_gid"
  LINE="── DOCKER_GID=${{GID}}  (from ${{SOCK}}; in-container probe failed — using host-stat)"
fi
printf '%s\\n' "$GID"
printf '%s\\n' "$LINE"
"""
    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=str(repo),
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or "cannot derive DOCKER_GID")
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        raise RuntimeError("DOCKER_GID discovery returned incomplete output")
    gid, line = lines[0].strip(), lines[1]
    if not gid.isdigit():
        raise RuntimeError(f"invalid DOCKER_GID: {gid!r}")
    return gid, line


def resolve_ws_host(repo: Path, env_path: Path) -> str:
    script = f"""
set -euo pipefail
source "{repo / "scripts/lib/stack_env.sh"}"
devcake_ws_host {env_path.as_posix()!r} {repo.as_posix()!r}
"""
    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=str(repo),
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "DEVCAKE_WS_HOST resolve failed").strip())
    ws = (proc.stdout or "").strip()
    if not ws.startswith("/"):
        raise RuntimeError(
            f"DEVCAKE_WS_HOST must be an absolute host path, got: {ws!r}"
        )
    return ws


def resolve_tag(env_path: Path) -> str:
    tag = os.environ.get("DEVCAKE_TAG", "").strip()
    if tag:
        return tag
    data = envfile.parse_env_file(env_path)
    tag = (data.get("DEVCAKE_TAG") or "").strip()
    return tag or "latest"


def _log(msg: str, *, as_json: bool) -> None:
    # Progress always on stderr when --json; otherwise human on stdout.
    stream = sys.stderr if as_json else sys.stdout
    stream.write(msg + "\n")
    stream.flush()


def prepare_env(
    repo: Path,
    opts: UpOptions,
    *,
    mutate: bool,
) -> tuple[UpPlan, str]:
    """Discover GID/WS/TAG, seed+auto-init .env. Returns (plan, gid_line)."""
    sock = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
    env_path = repo / ".env"
    example = repo / ".env.example"

    try:
        gid, gid_line = discover_docker_gid(repo, sock)
    except RuntimeError as exc:
        msg = str(exc)
        _log(msg if msg.startswith("error:") else f"error: {msg}", as_json=opts.as_json)
        raise SystemExit(3) from exc  # preflight

    if gid == "0":
        _log(
            "── WARNING: DOCKER_GID=0 grants the dagu service root-group access to the\n"
            "   Docker socket (root-equivalent control of the engine host — see\n"
            "   docs/14-security.md). Any docker.sock grant is already root-equivalent;\n"
            "   this is not a new privilege class. Continuing non-interactively.",
            as_json=opts.as_json,
        )

    ws_host = resolve_ws_host(repo, env_path)
    tag = resolve_tag(env_path)
    _log(gid_line, as_json=opts.as_json)
    _log(f"── DEVCAKE_WS_HOST={ws_host}", as_json=opts.as_json)
    _log(f"── DEVCAKE_TAG={tag}  (bake + compose lockstep)", as_json=opts.as_json)

    env_seeded = False
    env_generated: list[str] = []

    if not env_path.is_file():
        if not example.is_file():
            _log(
                "error: no .env and no .env.example — create .env with bootstrap passwords first",
                as_json=opts.as_json,
            )
            raise SystemExit(3)
        _log("── creating .env from .env.example", as_json=opts.as_json)
        if mutate:
            envfile.seed_env_from_example(env_path, example)
            env_seeded = True
        else:
            env_seeded = True  # would seed

    if mutate and env_path.is_file():
        env_generated = envfile.auto_init_bootstrap(env_path)
        if env_generated:
            _log(
                f"── auto-init generated bootstrap keys: {', '.join(env_generated)}",
                as_json=opts.as_json,
            )
        try:
            envfile.validate_oo_passwords(env_path)
        except ValueError as exc:
            _log(f"error: {exc}", as_json=opts.as_json)
            raise SystemExit(3) from exc
        envfile.upsert_env_var("DOCKER_GID", gid, env_path)
        envfile.upsert_env_var("DEVCAKE_WS_HOST", ws_host, env_path)
        envfile.upsert_env_var("DEVCAKE_TAG", tag, env_path)
        envfile.ensure_permission_floor(env_path)
        Path(ws_host).mkdir(parents=True, exist_ok=True)
        os.chmod(ws_host, 0o700)
    elif not mutate and env_path.is_file():
        # dry-run: still report what auto-init would generate without writing
        data = envfile.parse_env_file(env_path)
        for key in envfile.REQUIRED_BOOTSTRAP_KEYS:
            proc_val = os.environ.get(key)
            if proc_val is not None and not envfile.needs_generation(key, proc_val):
                continue
            existing = data.get(key, "")
            if envfile.needs_generation(key, existing):
                env_generated.append(key)

    plan = UpPlan(
        docker_gid=gid,
        ws_host=ws_host,
        tag=tag,
        sock=sock,
        bake=opts.bake,
        bake_targets=list(opts.bake_targets),
        compose_services=list(opts.compose_services),
        foreground_baker=opts.foreground_baker,
        no_hello_smoke=opts.no_hello_smoke,
        env_seeded=env_seeded,
        env_generated=env_generated,
    )
    return plan, gid_line


def _print_dry_run(plan: UpPlan, *, as_json: bool) -> None:
    if as_json:
        payload = {
            "ok": True,
            "schema_version": 1,
            "dry_run": True,
            "docker_gid": plan.docker_gid,
            "devcake_ws_host": plan.ws_host,
            "devcake_tag": plan.tag,
            "bake": plan.bake,
            "bake_targets": plan.bake_targets or ["app", "admin", "hello"],
            "compose_services": plan.compose_services,
            "foreground_baker": plan.foreground_baker,
            "no_hello_smoke": plan.no_hello_smoke,
            "env_seeded": plan.env_seeded,
            "env_generated": plan.env_generated,
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return
    _log(f"── would upsert DOCKER_GID={plan.docker_gid} in .env", as_json=False)
    _log(
        f"── would upsert DEVCAKE_WS_HOST={plan.ws_host} in .env (+ mkdir -p, chmod 700)",
        as_json=False,
    )
    _log(f"── would upsert DEVCAKE_TAG={plan.tag} in .env", as_json=False)
    if plan.env_generated:
        _log(
            f"── would auto-init bootstrap keys: {', '.join(plan.env_generated)}",
            as_json=False,
        )
    if plan.bake:
        _log("── would: docker compose stop dagu (deploy window — ADR-0025 R9)", as_json=False)
        _log("── would: compute DEVCAKE_APP_DIGEST from scripts/app_digest.py", as_json=False)
        targets = " ".join(plan.bake_targets) if plan.bake_targets else "app admin hello"
        _log(f"── would: DEVCAKE_TAG={plan.tag} docker buildx bake {targets}", as_json=False)
        if plan.no_hello_smoke:
            _log("── would: skip hello dispatch smoke (--no-hello-smoke)", as_json=False)
        else:
            _log("── would: hello dispatch smoke (scripts/ci_dispatch_hello.sh)", as_json=False)
    services = " ".join(plan.compose_services) if plan.compose_services else ""
    _log(f"── would: docker compose up -d {services}".rstrip(), as_json=False)
    if plan.foreground_baker:
        _log(
            "── would: run host baker in foreground (exec `devcake baker run`; no supervisor)",
            as_json=False,
        )
    else:
        _log(
            "── would: start host baker detached (launchd / systemd --user / flock respawn; "
            ".factory/watch.pid) — not a compose service",
            as_json=False,
        )


def _compose_env(plan: UpPlan) -> dict[str, str]:
    env = os.environ.copy()
    env["DOCKER_GID"] = plan.docker_gid
    env["DEVCAKE_WS_HOST"] = plan.ws_host
    env["DEVCAKE_TAG"] = plan.tag
    return env


def _bake(repo: Path, plan: UpPlan, *, as_json: bool) -> None:
    env = _compose_env(plan)
    # Deploy window: stop dagu before multi-minute bake (ADR-0025 R9).
    ps = subprocess.run(
        ["docker", "compose", "ps", "-q", "dagu"],
        cwd=str(repo),
        env=env,
        text=True,
        capture_output=True,
    )
    dagu_was_up = bool((ps.stdout or "").strip())
    restore_needed = False

    def _restore_dagu(*_args: object) -> None:
        nonlocal restore_needed
        if not restore_needed:
            return
        _log(
            "── bake interrupted/failed: restarting dagu (half-down stack guard)",
            as_json=as_json,
        )
        subprocess.run(
            ["docker", "compose", "start", "dagu"],
            cwd=str(repo),
            env=env,
            check=False,
        )
        restore_needed = False

    prev_sigint = signal.getsignal(signal.SIGINT)
    prev_sigterm = signal.getsignal(signal.SIGTERM)

    def _on_interrupt(signum: int, frame: object) -> None:
        _restore_dagu()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    if dagu_was_up:
        _log(
            "── stopping dagu before bake (deploy window — ADR-0025 R9)",
            as_json=as_json,
        )
        subprocess.run(
            ["docker", "compose", "stop", "dagu"],
            cwd=str(repo),
            env=env,
            check=False,
        )
        restore_needed = True
        signal.signal(signal.SIGINT, _on_interrupt)
        signal.signal(signal.SIGTERM, _on_interrupt)

    try:
        digest_proc = subprocess.run(
            [sys.executable, str(repo / "scripts" / "app_digest.py")],
            cwd=str(repo),
            text=True,
            capture_output=True,
            check=True,
        )
        digest = (digest_proc.stdout or "").strip()
        env["DEVCAKE_APP_DIGEST"] = digest
        _log(f"── DEVCAKE_APP_DIGEST={digest}", as_json=as_json)
        targets = plan.bake_targets or ["app", "admin", "hello"]
        _log(f"── docker buildx bake {' '.join(targets)}", as_json=as_json)
        bake = subprocess.run(
            ["docker", "buildx", "bake", *targets],
            cwd=str(repo),
            env=env,
        )
        if bake.returncode != 0:
            _restore_dagu()
            raise SystemExit(4)
    except Exception:
        _restore_dagu()
        raise
    finally:
        restore_needed = False
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)


def _compose_up(repo: Path, plan: UpPlan, *, as_json: bool) -> None:
    env = _compose_env(plan)
    argv = ["docker", "compose", "up", "-d", *plan.compose_services]
    _log("── " + " ".join(argv[0:4] + (plan.compose_services or [])), as_json=as_json)
    proc = subprocess.run(argv, cwd=str(repo), env=env)
    if proc.returncode != 0:
        raise SystemExit(4)


def _health_gate(repo: Path, plan: UpPlan, *, as_json: bool) -> None:
    env = _compose_env(plan)
    _log("── waiting for the app to report healthy…", as_json=as_json)
    live_py = (
        "import urllib.request as u; "
        "u.urlopen('http://localhost:8000/api/v1/health/live', timeout=3)"
    )
    ok = False
    for _ in range(30):
        proc = subprocess.run(
            ["docker", "compose", "exec", "-T", "app", "python", "-c", live_py],
            cwd=str(repo),
            env=env,
            capture_output=True,
        )
        if proc.returncode == 0:
            ok = True
            break
        time.sleep(2)
    if ok:
        _log("── app live ✓", as_json=as_json)
        deps_py = """
import base64, json, os, urllib.request
u, p = os.environ.get("ADMIN_USER", ""), os.environ.get("ADMIN_PASSWORD", "")
tok = base64.b64encode(f"{u}:{p}".encode()).decode()
req = urllib.request.Request(
    "http://localhost:8000/api/v1/health",
    headers={"Authorization": f"Basic {tok}"})
body = json.loads(urllib.request.urlopen(req, timeout=10).read())
bad = [k for k in ("redis", "dagu") if body.get(k) is False]
raise SystemExit(1 if bad else 0)
"""
        deps = subprocess.run(
            ["docker", "compose", "exec", "-T", "app", "python", "-c", deps_py],
            cwd=str(repo),
            env=env,
            capture_output=True,
        )
        if deps.returncode == 0:
            _log("── app redis+dagu probes ok ✓", as_json=as_json)
        else:
            _log(
                "── WARNING: app is live but redis/dagu probe is red — check: "
                "docker compose logs --tail=50 app",
                as_json=as_json,
            )
    else:
        _log(
            "── WARNING: app did not report live within ~60s. The stack is up,\n"
            "   but the app may be wedged — check: docker compose logs --tail=50 app\n"
            "   (OpenObserve crash-loop on a weak root password? also: "
            "docker compose logs openobserve)",
            as_json=as_json,
        )

    # Fatal: dagu sock writability as uid 1000 / gid DOCKER_GID
    _log("── verifying dagu can write the Docker socket…", as_json=as_json)
    sock_ok = False
    for _ in range(15):
        proc = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "--user",
                f"1000:{plan.docker_gid}",
                "dagu",
                "sh",
                "-c",
                "test -w /var/run/docker.sock",
            ],
            cwd=str(repo),
            env=env,
            capture_output=True,
        )
        if proc.returncode == 0:
            sock_ok = True
            break
        time.sleep(2)
    if not sock_ok:
        obs = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "dagu",
                "sh",
                "-c",
                "stat -c %g /var/run/docker.sock",
            ],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
        )
        obs_gid = (obs.stdout or "").strip() or "unknown"
        _log(
            f"error: dagu cannot write /var/run/docker.sock "
            f"(resolved DOCKER_GID={plan.docker_gid}; socket gid inside the "
            f"container={obs_gid}).\n"
            f"Fix: set the gid the container actually sees, e.g. in "
            f"docker-compose.override.yml:\n\n"
            f"services:\n"
            f"  dagu:\n"
            f"    environment:\n"
            f'      DOCKER_GID: "0"\n\n'
            f"Then re-run: devcake up",
            as_json=as_json,
        )
        raise SystemExit(4)
    _log("── dagu docker.sock writable ✓", as_json=as_json)


def _hello_smoke(repo: Path, plan: UpPlan, *, as_json: bool) -> None:
    if plan.no_hello_smoke:
        _log("── skipping hello dispatch smoke (--no-hello-smoke)", as_json=as_json)
        return
    data = envfile.parse_env_file(repo / ".env")
    user = data.get("ADMIN_USER", "")
    password = data.get("ADMIN_PASSWORD", "")
    if not user or not password:
        _log(
            "── WARNING: skipping hello dispatch smoke — ADMIN_USER / "
            "ADMIN_PASSWORD missing from .env",
            as_json=as_json,
        )
        return
    _log("── hello dispatch smoke (scripts/ci_dispatch_hello.sh)…", as_json=as_json)
    env = _compose_env(plan)
    env["ADMIN_USER"] = user
    env["ADMIN_PASSWORD"] = password
    proc = subprocess.run(
        [str(repo / "scripts" / "ci_dispatch_hello.sh")],
        cwd=str(repo),
        env=env,
    )
    if proc.returncode != 0:
        _log(
            "── ERROR: hello dispatch smoke failed — the stack is up but Dagu\n"
            "   cannot complete a Dev container run. Check:\n"
            "     docker compose logs --tail=50 dagu\n"
            "   Look for the preceding 'hello run_id=…' line for the run id.\n"
            "   Known cause: Docker-socket permissions (Docker Desktop hosts especially).",
            as_json=as_json,
        )
        raise SystemExit(4)


def _start_baker(repo: Path, plan: UpPlan, *, as_json: bool) -> None:
    factory = repo / ".factory"
    factory.mkdir(parents=True, exist_ok=True)
    pidfile = factory / "watch.pid"
    logfile = factory / "watch.log"
    env = _compose_env(plan)
    data = envfile.parse_env_file(repo / ".env")
    for key in ("OO_INGEST_EMAIL", "OO_INGEST_PASSWORD", "OO_ORG"):
        if key in data and data[key]:
            env[key] = data[key]
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = f"{repo / 'scripts'}:{repo / 'app'}"
    env["DEVCAKE_OO_URL"] = "http://127.0.0.1:5080"
    env["DEVCAKE_FACTORY_DIR"] = str(factory)
    env["DEVCAKE_FACTORY_LOG"] = str(logfile)

    # prepare pidfile + displace via baker_host.sh chokepoint
    prep = f"""
set -euo pipefail
source "{repo / "scripts/lib/baker_host.sh"}"
devcake_baker_prepare_pidfile {pidfile.as_posix()!r}
devcake_baker_displace_orphans {factory.as_posix()!r}
"""
    proc = subprocess.run(["bash", "-c", prep], cwd=str(repo), env=env, text=True)
    if proc.returncode != 0:
        raise SystemExit(6)

    if plan.foreground_baker:
        resolve = f"""
set -euo pipefail
source "{repo / "scripts/lib/baker_host.sh"}"
devcake_baker_resolve_entry
"""
        entry = subprocess.run(
            ["bash", "-c", resolve],
            cwd=str(repo),
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        cmd = (entry.stdout or "").strip()
        _log(
            f"── host baker in foreground (pidfile {pidfile}; Ctrl-C to stop)",
            as_json=as_json,
        )
        _log(
            "── stack up (admin: http://localhost:8080); baker takes this terminal",
            as_json=as_json,
        )
        pidfile.write_text(f"{os.getpid()}\n", encoding="utf-8")
        # Replace this process with the baker entry (shell-words).
        os.execvp("bash", ["bash", "-c", f"exec {cmd}"])

    if not logfile.is_file():
        logfile.write_text("", encoding="utf-8")
    baseline = logfile.stat().st_size

    install = f"""
set -euo pipefail
source "{repo / "scripts/lib/baker_host.sh"}"
REPO={repo.as_posix()!r}
FACTORY={factory.as_posix()!r}
LOG={logfile.as_posix()!r}
PIDFILE={pidfile.as_posix()!r}
BASELINE={baseline}
PLAT="$(devcake_baker_platform)"
SUPERVISED=0
LAUNCH=""
PID=""
if [[ "$PLAT" == "darwin" ]]; then
  if devcake_baker_launchd_available \\
    && devcake_baker_launchd_install "$REPO" "$FACTORY" "$LOG" "$PIDFILE"; then
    PID="$(cat "$PIDFILE" 2>/dev/null || true)"
    LAUNCH="launchctl kickstart gui/$(id -u)/${{DEVCAKE_BAKER_LAUNCHD_LABEL:-com.devcake.baker}}"
    SUPERVISED=1
  fi
elif [[ "$PLAT" == "linux" ]] && devcake_baker_systemd_available; then
  if devcake_baker_systemd_install "$REPO" "$FACTORY" "$LOG" "$PIDFILE"; then
    PID="$(cat "$PIDFILE" 2>/dev/null || true)"
    LAUNCH="systemctl --user start ${{DEVCAKE_BAKER_UNIT:-devcake-baker.service}}"
    SUPERVISED=1
  else
    systemctl --user stop "${{DEVCAKE_BAKER_UNIT:-devcake-baker.service}}" \\
      >/dev/null 2>&1 || true
  fi
fi
if [[ "$SUPERVISED" -eq 0 ]]; then
  case "$PLAT" in
    darwin) devcake_baker_degraded_gap "launchd install/start failed" ;;
    linux)  devcake_baker_degraded_gap "$(devcake_baker_linux_degraded_reason)" ;;
    *)      devcake_baker_degraded_gap "platform ${{PLAT}} has no native supervisor" ;;
  esac
  if ! devcake_baker_respawn_install "$REPO" "$FACTORY" "$LOG" "$PIDFILE"; then
    echo "── failed to install flock-guarded baker respawn supervisor" >&2
    exit 6
  fi
  PID="$(cat "$PIDFILE" 2>/dev/null || true)"
  LAUNCH="baker_respawn.sh $REPO $FACTORY"
fi
devcake_baker_wait_liveness "$PID" "$LOG" "$PIDFILE" "$LAUNCH" 12 "$BASELINE"
"""
    proc = subprocess.run(["bash", "-c", install], cwd=str(repo), env=env)
    if proc.returncode != 0:
        raise SystemExit(6)
    _log("── stack starting (admin: http://localhost:8080)", as_json=as_json)
    _log(
        "   bootstrap passwords come from .env (auto-init fills empties); "
        "operator secrets via Config.",
        as_json=as_json,
    )


def run_up(opts: UpOptions, *, repo: Path | None = None) -> int:
    try:
        root = repo or require_checkout_root()
    except FileNotFoundError as exc:
        sys.stderr.write(f"devcake up: {exc}\n")
        return 3

    try:
        plan, _ = prepare_env(root, opts, mutate=not opts.dry_run)
    except SystemExit as exc:
        return int(exc.code or 1)

    if opts.dry_run:
        _print_dry_run(plan, as_json=opts.as_json)
        return 0

    try:
        if plan.bake:
            _bake(root, plan, as_json=opts.as_json)
        _compose_up(root, plan, as_json=opts.as_json)
        _health_gate(root, plan, as_json=opts.as_json)
        if plan.bake:
            _hello_smoke(root, plan, as_json=opts.as_json)
        _start_baker(root, plan, as_json=opts.as_json)
    except SystemExit as exc:
        return int(exc.code or 1)

    if opts.as_json:
        payload = {
            "ok": True,
            "schema_version": 1,
            "dry_run": False,
            "docker_gid": plan.docker_gid,
            "devcake_ws_host": plan.ws_host,
            "devcake_tag": plan.tag,
            "bake": plan.bake,
            "env_seeded": plan.env_seeded,
            "env_generated": plan.env_generated,
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return 0
