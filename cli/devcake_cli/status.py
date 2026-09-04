"""``devcake status`` — compose project + baker liveness snapshot, plus the
PMO request budgets the app measures (ADR-0040) when the stack answers."""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .doctor import check_baker_liveness
from .paths import require_checkout_root
from .setup import _basic_auth_header, _load_admin_auth

# the admin proxy is pinned to loopback in docker-compose.yml (docs/14)
ADMIN_URL = "http://127.0.0.1:8080"
# a cold /health runs its tracker and forge probes first (up to ~5 s each)
HEALTH_TIMEOUT_S = 10.0


def _fetch_health(root: Path, *, timeout: float = HEALTH_TIMEOUT_S,
                  ) -> tuple[dict | None, str | None]:
    """(payload, None) — the app's /health via the loopback admin proxy,
    authenticated with the checkout's admin credentials — or (None, reason).
    A stack that does not answer is a status line, never a traceback."""
    try:
        user, password = _load_admin_auth(root)
    except (RuntimeError, OSError) as exc:
        return None, str(exc)
    req = urllib.request.Request(
        f"{ADMIN_URL}/api/v1/health",
        headers={"X-DevCake-Request": "1",
                 "Authorization": _basic_auth_header(user, password)})
    # never route a loopback call — and the admin password — through an
    # http_proxy from the environment
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310 — loopback, fixed scheme
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        hint = (" — check ADMIN_USER / ADMIN_PASSWORD in .env"
                if exc.code in (401, 403) else "")
        return None, f"the admin proxy answered HTTP {exc.code}{hint}"
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return None, (f"the admin proxy at {ADMIN_URL} did not answer within "
                      f"{timeout:g} s ({exc.__class__.__name__}) — stack down?")
    if not isinstance(body, dict):
        return None, "the admin proxy returned something other than the health payload"
    return body, None


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
        remaining = row.get("remaining_estimate")
        if remaining is None:
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
    health, health_error = _fetch_health(root)
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
        "health_error": health_error,
        "pmo_budget": (health or {}).get("pmo_budget") if health else None,
        "pmo_rate_limited": ((health or {}).get("pmo_rate_limited") or {}
                             if health else None),
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
            sys.stdout.write(f"pmo_budget: unavailable ({health_error})\n")
        else:
            lines = budget_lines(health)
            sys.stdout.write("pmo_budget:" + ("" if lines else " (no PMO "
                             "connection has made a request yet)") + "\n")
            for line in lines:
                sys.stdout.write(line + "\n")
    return 0 if compose_ok else 4
