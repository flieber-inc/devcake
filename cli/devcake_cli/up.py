"""``devcake up`` — the stack bring-up verb (ADR-0038 Decision 1)."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from . import envfile
from .doctor import (APPARMOR_PROFILE_NAME, DOCKER_DEFAULT_PROFILE,
                     apparmor_facts, apparmor_install_commands)
from .paths import read_version_pin, require_checkout_root


# What `devcake up --bake` may build: the control plane (and the CI test
# image). Dev images are the host baker's alone — the keep-set is the order,
# receipts are the registrar — and `up` never bakes one, not on request:
# a bake here would race the baker's own build of the same pin and burn
# minutes on house-pin images no Dev Type dispatches on (ADR-0038 addendum).
CONTROL_PLANE_TARGETS = ("app", "admin", "hello", "app-test")
DEFAULT_BAKE_TARGETS = ("app", "admin", "hello")


def refused_bake_targets(targets) -> str | None:
    """The refusal line when `targets` names anything but the control
    plane (a harness, `all`, `images`), else None."""
    bad = [t for t in targets if t not in CONTROL_PLANE_TARGETS]
    if not bad:
        return None
    return (f"--bake {' '.join(bad)}: devcake up bakes the control plane only "
            f"({' '.join(DEFAULT_BAKE_TARGETS)}; app-test for CI). Dev images are "
            "the host baker's — save a Dev Type to order one, or run "
            "`docker buildx bake <target>` by hand for a development build")


@dataclass
class UpOptions:
    bake: bool = False
    bake_targets: list[str] = field(default_factory=list)
    dry_run: bool = False
    foreground_baker: bool = False
    no_hello_smoke: bool = False
    compose_services: list[str] = field(default_factory=list)
    as_json: bool = False
    # --release [TAG]: check the release out first (None = not requested);
    # implies --bake (the control plane) unless --bake was given, and a
    # stale-image tidy-up after a successful bring-up (never Dev images —
    # the baker's, which rebuilds the pinned ones itself on a tag move)
    release: str | None = None


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
    release_requested: bool = False
    # the AppArmor profile dev-run.yaml names on both Dev steps — derived
    # here at up time (a snapshot: load the profile, run devcake up again)
    apparmor_profile: str = DOCKER_DEFAULT_PROFILE


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


def resolve_tag(env_path: Path, repo: Path | None = None) -> str:
    """The image tag bake and compose run under. The checkout's `VERSION`
    file is the pin — cutting a release bumps it together with the
    changelog — so an operator never edits `.env` for it; `devcake up`
    writes the resolved value INTO `.env` so a later plain `docker compose`
    stays lockstep. A `DEVCAKE_TAG` in the process environment overrides it
    for development builds (a short sha, a scratch label). `.env`'s own
    value is never a source: a stale pin there is rewritten, not obeyed."""
    tag = os.environ.get("DEVCAKE_TAG", "").strip()
    if tag:
        return tag
    pinned = read_version_pin(repo) if repo is not None else ""
    return pinned or "latest"


def stale_env_tag(env_path: Path, tag: str) -> str:
    """A `DEVCAKE_TAG` in `.env` that disagrees with the resolved tag, or ""."""
    if not env_path.is_file():
        return ""
    data = envfile.parse_env_file(env_path)
    stale = (data.get("DEVCAKE_TAG") or "").strip()
    return stale if stale and stale != tag else ""


def derive_apparmor_profile(repo: Path) -> tuple[str, str, str]:
    """(value for DEVCAKE_APPARMOR_PROFILE, one status line, warning or "").
    Never refuses: a host without the profile still launches runs — under
    docker-default the nested engine inside Devs is unavailable, and the
    bake receipt, `devcake status` and the Dev's prompt all say so."""
    f = apparmor_facts(repo_root=repo)
    if not f.enabled:
        return (DOCKER_DEFAULT_PROFILE,
                f"── DEVCAKE_APPARMOR_PROFILE={DOCKER_DEFAULT_PROFILE}  "
                "(no AppArmor on the Docker host)", "")
    cmds = apparmor_install_commands(repo)
    if not f.usable:
        need = ("" if f.parser else
                "install the apparmor package (apparmor_parser) first, then ")
        why = ("rejected by this host's apparmor_parser (4.0 or newer is needed)"
               if f.compiles is False else
               "installed but not loaded by the kernel"
               if f.applies is False and f.installed else
               "not loaded")
        return (DOCKER_DEFAULT_PROFILE,
                f"── DEVCAKE_APPARMOR_PROFILE={DOCKER_DEFAULT_PROFILE}  "
                f"(AppArmor active, {APPARMOR_PROFILE_NAME} {why})",
                "── WARNING: nested containers inside Dev containers are unavailable\n"
                f"   on this host until the {APPARMOR_PROFILE_NAME} AppArmor profile is\n"
                "   loaded. One-time fix (printed only; this CLI will not run it):\n"
                f"   {need}{cmds}\n"
                "   then run devcake up again.")
    how = ("the daemon applies it" if f.applies else
           "loaded" if f.loaded else
           "installed and compiled by this host's parser; loaded state unreadable without root")
    warn = ""
    if f.current is False:
        warn = (f"── WARNING: the loaded {APPARMOR_PROFILE_NAME} profile differs from\n"
                "   this checkout's. Re-run (printed only; this CLI will not run it):\n"
                f"   {cmds}")
    return (APPARMOR_PROFILE_NAME,
            f"── DEVCAKE_APPARMOR_PROFILE={APPARMOR_PROFILE_NAME}  ({how})", warn)


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
    tag = resolve_tag(env_path, repo)
    _log(gid_line, as_json=opts.as_json)
    _log(f"── DEVCAKE_WS_HOST={ws_host}", as_json=opts.as_json)
    source = ("process env" if os.environ.get("DEVCAKE_TAG", "").strip()
              else "the checkout's VERSION" if read_version_pin(repo) else "default")
    _log(f"── DEVCAKE_TAG={tag}  ({source}; bake + compose lockstep)",
         as_json=opts.as_json)
    stale = stale_env_tag(env_path, tag)
    if stale:
        _log(f"── .env carried DEVCAKE_TAG={stale}; the pin lives in the "
             f"checkout now and .env is rewritten to {tag}", as_json=opts.as_json)
    apparmor_profile, apparmor_line, apparmor_warn = derive_apparmor_profile(repo)
    _log(apparmor_line, as_json=opts.as_json)
    if apparmor_warn:
        _log(apparmor_warn, as_json=opts.as_json)

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
        envfile.upsert_env_var("DEVCAKE_APPARMOR_PROFILE", apparmor_profile, env_path)
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
        release_requested=opts.release is not None,
        apparmor_profile=apparmor_profile,
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
            "devcake_apparmor_profile": plan.apparmor_profile,
            "bake": plan.bake,
            "bake_targets": plan.bake_targets or list(DEFAULT_BAKE_TARGETS),
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
    _log(f"── would upsert DEVCAKE_APPARMOR_PROFILE={plan.apparmor_profile} in .env",
         as_json=False)
    if plan.env_generated:
        _log(
            f"── would auto-init bootstrap keys: {', '.join(plan.env_generated)}",
            as_json=False,
        )
    if plan.bake:
        _log("── would: docker compose stop dagu (deploy window — ADR-0025 R9)", as_json=False)
        _log("── would: compute DEVCAKE_APP_DIGEST from scripts/app_digest.py", as_json=False)
        targets = " ".join(plan.bake_targets or DEFAULT_BAKE_TARGETS)
        _log(f"── would: DEVCAKE_TAG={plan.tag} docker buildx bake {targets}", as_json=False)
        if plan.no_hello_smoke:
            _log("── would: skip hello dispatch smoke (--no-hello-smoke)", as_json=False)
        else:
            _log("── would: hello dispatch smoke (scripts/ci_dispatch_hello.sh)", as_json=False)
    services = " ".join(plan.compose_services) if plan.compose_services else ""
    if not plan.foreground_baker:
        _log(
            "── would: replace host baker detached (launchd / systemd --user / flock respawn; "
            ".factory/watch.pid) — before the app is recreated, so it claims the "
            "app's bake order — not a compose service",
            as_json=False,
        )
    _log(f"── would: docker compose up -d {services}".rstrip(), as_json=False)
    if plan.release_requested:
        _log(f"── would: remove stale control-plane images not on {plan.tag} and "
             "dangling leftovers (never Dev images)", as_json=False)
    if plan.foreground_baker:
        _log(
            "── would: run host baker in foreground (exec `devcake baker run`; no supervisor)",
            as_json=False,
        )


def _compose_env(plan: UpPlan) -> dict[str, str]:
    env = os.environ.copy()
    env["DOCKER_GID"] = plan.docker_gid
    env["DEVCAKE_WS_HOST"] = plan.ws_host
    env["DEVCAKE_TAG"] = plan.tag
    env["DEVCAKE_APPARMOR_PROFILE"] = plan.apparmor_profile
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
        targets = plan.bake_targets or list(DEFAULT_BAKE_TARGETS)
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


_DAGU_PIN = re.compile(r"ghcr\.io/dagucloud/dagu:([0-9][A-Za-z0-9._-]*)")


def dagu_pin_moved(compose_text: str, running_image: str) -> tuple[str, str] | None:
    """(running tag, pinned tag) when the checkout pins a Dagu release the
    running dagu service is not on; None when equal or unknown."""
    m = _DAGU_PIN.search(compose_text or "")
    r = _DAGU_PIN.search(running_image or "")
    if not m or not r or m.group(1) == r.group(1):
        return None
    return r.group(1), m.group(1)


# The helper image the shipped backup scripts use (GNU tar; digest-pinned,
# same as scripts/backup_data.sh).
_BACKUP_IMAGE = ("debian:bookworm-slim@sha256:"
                 "88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171")


def _dagu_volume(repo: Path) -> str | None:
    try:
        proc = subprocess.run(["docker", "volume", "ls", "--format", "{{.Name}}"],
                              cwd=str(repo), text=True, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    names = [n.strip() for n in (proc.stdout or "").splitlines()
             if n.strip().endswith("_dagu_data")]
    return names[0] if len(names) == 1 else None


def _running_dagu_image(repo: Path) -> str:
    """The image the dagu service last ran — stopped containers included
    (`ps -a`: an operator who ran `devcake down` first has no running
    one). Empty when compose knows no dagu container at all."""
    ps = subprocess.run(
        ["docker", "compose", "ps", "-a", "--format", "json", "dagu"],
        cwd=str(repo), text=True, capture_output=True, timeout=60)
    rows: list = []
    text = (ps.stdout or "").strip()
    if text.startswith("["):                      # older compose: one array
        rows = [r for r in json.loads(text) if isinstance(r, dict)]
    else:                                         # newer: one object per line
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                rows.append(json.loads(line))
    return str(rows[0].get("Image") or "") if rows else ""


def _dagu_backup(repo: Path, *, as_json: bool, dry_run: bool = False) -> None:
    """Archive the Dagu state volume under .factory/backups/ BEFORE a
    re-pinned Dagu starts and migrates it (docs/13 §4): the archive is the
    rollback path — an older Dagu cannot read a migrated store. A backup,
    not a migration: Dagu does its own on first start; nothing here reads
    the archive back. Taken whenever the checkout pins a Dagu the volume
    was not last used with — and, when compose knows no dagu container at
    all (the stack was taken down), whenever the volume exists, since the
    previous version is then unknown. The archive holds run params and
    step output in clear (docs/14 §9), so it is written 0600, owned by the
    operator, in a 0700 directory; the newest three are kept. dagu is
    stopped first (a quiet copy), the bring-up restarts it. A failed
    archive warns and the bring-up continues; the printed command archives
    by hand."""
    try:
        running = _running_dagu_image(repo)
        pinned = _DAGU_PIN.search((repo / "docker-compose.yml").read_text())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return
    if not pinned:
        return
    volume = _dagu_volume(repo)
    if running:
        moved = dagu_pin_moved((repo / "docker-compose.yml").read_text(), running)
        if not moved:
            return
        head = (f"── this release re-pins Dagu {moved[0]} → {moved[1]}; its state "
                "store is migrated on first start.")
    elif volume:
        head = (f"── no dagu container is known (stack down?) and the checkout pins "
                f"Dagu {pinned.group(1)}; the store's previous version is unknown, "
                "so it is archived first.")
    else:
        return
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest_dir = repo / ".factory" / "backups"
    archive = dest_dir / f"dagu_data-{stamp}.tgz"
    uid, gid = os.getuid(), os.getgid()
    inner = (f"umask 077 && tar czf /to/{archive.name} -C /from . "
             f"&& chown {uid}:{gid} /to/{archive.name}")
    cmd = ["docker", "run", "--rm", "--network", "none",
           "--mount", f"type=volume,src={volume or '<project>_dagu_data'},dst=/from,readonly",
           "--mount", f"type=bind,src={dest_dir},dst=/to",
           _BACKUP_IMAGE, "sh", "-c", inner]
    shown = " ".join(cmd[:-1]) + " " + repr(inner)
    if dry_run or volume is None:
        why = ("" if volume else
               " (the Dagu volume could not be identified — archive by hand)")
        _log(f"{head} {'Would archive' if dry_run else 'Archive'} it{why}:\n   {shown}",
             as_json=as_json)
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_dir, 0o700)
    _log(f"{head} Stopping dagu and archiving → {archive}", as_json=as_json)
    subprocess.run(["docker", "compose", "stop", "dagu"], cwd=str(repo),
                   capture_output=True, timeout=120)
    err = ""
    try:
        proc = subprocess.run(cmd, cwd=str(repo), text=True, capture_output=True,
                              timeout=600)
        err = (proc.stderr or "").strip()
        ok = proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok, err = False, str(exc)
    if not ok or not archive.is_file():
        _log("── WARNING: the Dagu archive failed"
             + (f" ({err[:200]})" if err else "")
             + " — continuing; rolling Dagu back needs an archive, so take one "
             "by hand now if the run history matters:\n   " + shown,
             as_json=as_json)
        return
    os.chmod(archive, 0o600)
    for old in sorted(dest_dir.glob("dagu_data-*.tgz"))[:-3]:
        try:
            old.unlink()
        except OSError:
            pass
    _log(f"── Dagu state archived ({archive.stat().st_size // 1024} KiB, 0600); "
         "rollback: docs/13 §8", as_json=as_json)


def _prune_after_release(repo: Path, plan: UpPlan, *, as_json: bool) -> None:
    from .prune import prune_images
    try:
        prune_images(repo, tag=plan.tag, as_json=as_json)
    except RuntimeError as exc:   # a failed listing never fails the bring-up
        _log(f"── image tidy-up skipped: {exc}", as_json=as_json)


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

    # prepare pidfile + displace via baker_host.sh chokepoint. Order matters:
    # a degraded respawn supervisor goes FIRST (and is waited for), or it
    # respawns the baker we are about to kill straight into the handoff and
    # its orphans hold the respawn lock the successor needs (2026-09-01).
    prep = f"""
set -euo pipefail
source "{repo / "scripts/lib/baker_host.sh"}"
devcake_baker_stop_respawn_supervisor {factory.as_posix()!r}
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

    # before anything moves — including the release checkout: Dev images
    # are the host baker's, `up` bakes the control plane only
    refused = refused_bake_targets(opts.bake_targets) if opts.bake else None
    if refused:
        sys.stderr.write(f"devcake up: {refused}\n")
        return 2

    if opts.release is not None:
        # before anything else: the tag resolution below reads the
        # checkout's VERSION, so the release must be checked out first
        from . import release as release_mod
        try:
            release_mod.checkout_release(root, opts.release, dry_run=opts.dry_run,
                                         as_json=opts.as_json)
        except release_mod.ReleaseRefused as exc:
            sys.stderr.write(f"devcake up --release: {exc}\n")
            return 6
        if not opts.bake:
            opts.bake = True          # the control plane; never a Dev image
            opts.bake_targets = []

    # a re-pinned Dagu (a release, or a plain pull) migrates its store on
    # first start: archive it first, whatever brought the new pin here
    _dagu_backup(root, as_json=opts.as_json, dry_run=opts.dry_run)

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
        # The host baker is replaced BEFORE the app is recreated. The app
        # publishes its one-shot bake order (the keep-set) at boot; with
        # the baker replaced afterwards, the outgoing baker — still ticking
        # under the previous tag — claimed that order, and the incoming one
        # found an empty inbox, dropped the previous tag's receipts (their
        # images are named with that tag) and had nothing to rebuild: every
        # Dev Type "waiting — no receipt" until someone re-saved a Dev Type.
        # Replaced first, the incoming baker is the one that claims the
        # order; the inbox is durable until then, and the baker tolerates
        # the app being down or on the previous digest for minutes (docs/13).
        # The foreground variant execs the baker and never returns, so it
        # stays last.
        if not plan.foreground_baker:
            _start_baker(root, plan, as_json=opts.as_json)
        _compose_up(root, plan, as_json=opts.as_json)
        _health_gate(root, plan, as_json=opts.as_json)
        if plan.bake:
            _hello_smoke(root, plan, as_json=opts.as_json)
        if opts.release is not None:
            # only after a successful bring-up: stale control-plane images
            # and dangling leftovers; Dev images stay the baker's
            _prune_after_release(root, plan, as_json=opts.as_json)
        if plan.foreground_baker:
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
            "devcake_apparmor_profile": plan.apparmor_profile,
            "bake": plan.bake,
            "env_seeded": plan.env_seeded,
            "env_generated": plan.env_generated,
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return 0
