"""``devcake status`` — compose project + baker liveness snapshot, plus the
PMO request budgets the app measures (ADR-0040) when the stack answers."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .doctor import check_baker_liveness
from .envfile import parse_env_file
from .paths import require_checkout_root

# the admin proxy is pinned to loopback in docker-compose.yml (docs/14)
ADMIN_URL = "http://127.0.0.1:8080"
HEALTH_TIMEOUT_S = 5.0


def _fetch_health(root: Path, *, timeout: float = HEALTH_TIMEOUT_S) -> dict | None:
    """The app's /health via the loopback admin proxy, authenticated with
    the checkout's admin credentials. None when the stack does not answer —
    a status line, never a failure."""
    env_path = root / ".env"
    env = parse_env_file(env_path) if env_path.is_file() else {}
    req = urllib.request.Request(
        f"{ADMIN_URL}/api/v1/health", headers={"X-DevCake-Request": "1"})
    user, password = env.get("ADMIN_USER"), env.get("ADMIN_PASSWORD")
    if user and password:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — loopback, fixed scheme
            body = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return body if isinstance(body, dict) else None


def budget_lines(health: dict | None) -> list[str]:
    """One line per credential bucket (+ the alarm text when the tracker
    rejected requests in the last hour). Empty when /health carries none."""
    rows = (health or {}).get("pmo_budget") or {}
    alarms = (health or {}).get("pmo_rate_limited") or {}
    out: list[str] = []
    for label, row in sorted(rows.items()):
        demand = row.get("demand_per_hour") or {}
        total = sum(v for v in demand.values() if isinstance(v, int))
        per = ", ".join(
            f"{name} {v if isinstance(v, int) else 'measuring'}"
            for name, v in sorted(demand.items()))
        limit = row.get("limit")
        against = f"{limit}/hour" if limit else "no published limit"
        remaining = row.get("remaining")
        rem = f", {remaining} remaining" if remaining is not None else ""
        rejected = row.get("limited_last_hour") or 0
        line = (f"  {label}: about {total} requests/hour ({per}) against "
                f"{against}{rem}; rejected by the tracker in the last hour: "
                f"{rejected}")
        if row.get("blocked_until"):
            line += "; PAUSED by the tracker"
        out.append(line)
        if label in alarms:
            out.append(f"    ! {alarms[label]}")
    return out


def _compose_ps(repo: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            cwd=str(repo),
            text=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        # Fallback without --format for older compose
        proc2 = subprocess.run(
            ["docker", "compose", "ps"],
            cwd=str(repo),
            text=True,
            capture_output=True,
            timeout=60,
        )
        text = (proc2.stdout or proc2.stderr or "").strip()
        return proc2.returncode == 0, text
    return True, (proc.stdout or "").strip()


def run_status(*, as_json: bool = False, repo: Path | None = None) -> int:
    try:
        root = repo or require_checkout_root()
    except FileNotFoundError as exc:
        sys.stderr.write(f"devcake status: {exc}\n")
        return 3

    compose_ok, compose_text = _compose_ps(root)
    health = _fetch_health(root)
    baker = check_baker_liveness(repo_root=root)
    baker_alive = bool(baker.ok and "alive" in baker.detail and "dead" not in baker.detail)
    # Prefer explicit pid alive wording from check detail.
    if baker.detail.startswith("baker pid ") and "is alive" in baker.detail:
        baker_alive = True
    elif "not expected" in baker.detail or "skipped" in baker.detail:
        baker_alive = False

    payload = {
        "ok": compose_ok,
        "schema_version": 1,
        "compose_ok": compose_ok,
        "compose": compose_text,
        "baker_alive": baker_alive,
        "baker_detail": baker.detail,
        "checkout": str(root),
        # ADR-0040: what the app measures against each tracker's quota —
        # None when the stack did not answer (down, or credentials missing)
        "health_reachable": health is not None,
        "pmo_budget": (health or {}).get("pmo_budget") if health else None,
        "pmo_rate_limited": (health or {}).get("pmo_rate_limited") or {},
    }

    if as_json:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stdout.write(f"checkout: {root}\n")
        sys.stdout.write(f"compose: {'ok' if compose_ok else 'FAIL'}\n")
        if compose_text:
            sys.stdout.write(compose_text + "\n")
        sys.stdout.write(
            f"baker_alive: {baker_alive} ({baker.detail})\n"
        )
        if health is None:
            sys.stdout.write(
                "pmo_budget: unavailable (the admin proxy at "
                f"{ADMIN_URL} did not answer — stack down, or "
                "ADMIN_USER/ADMIN_PASSWORD missing from .env)\n")
        else:
            lines = budget_lines(health)
            sys.stdout.write("pmo_budget:" + ("" if lines else " (no PMO "
                             "connection has made a request yet)") + "\n")
            for line in lines:
                sys.stdout.write(line + "\n")
    return 0 if compose_ok else 4
