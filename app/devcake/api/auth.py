"""HTTP authentication and request-intent checks for the control-plane API."""

import base64
import binascii
import os
import re
import secrets
from contextvars import ContextVar

from fastapi import Request
from fastapi.responses import JSONResponse


LIVE_PATH = "/api/v1/health/live"
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Who is acting through the control plane, for the audit trail: "admin" for a
# plain authenticated request (the admin panel, curl), or the short label a
# client sends in X-DevCake-Actor (the operator MCP server sends "mcp",
# ADR-0041). Empty outside a request (poll cycle, finalize). Label only —
# never an identity claim: every request still authenticates the same way.
REQUEST_ACTOR: ContextVar[str] = ContextVar("devcake_request_actor", default="")
_ACTOR_RE = re.compile(r"[^a-z0-9._-]")


def request_actor(request) -> str:
    raw = (request.headers.get("x-devcake-actor") or "admin").lower()
    return (_ACTOR_RE.sub("", raw) or "admin")[:32]


def admin_credentials() -> tuple[str, str]:
    return os.environ.get("ADMIN_USER", ""), os.environ.get("ADMIN_PASSWORD", "")


def credentials_configured() -> bool:
    user, password = admin_credentials()
    return bool(user and password)


def _const_eq(a: str, b: str) -> bool:
    """Constant-time string equality that never raises (auth must stay 401).

    secrets.compare_digest on str rejects non-ASCII (TypeError → 500).
    Compare UTF-8 bytes instead; length mismatch returns False, not an exception.
    """
    try:
        return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def _valid_basic_auth(value: str) -> bool:
    if not value.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(value[6:], validate=True).decode("utf-8")
        supplied_user, supplied_password = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    expected_user, expected_password = admin_credentials()
    configured = bool(expected_user and expected_password)
    # BOTH comparisons always run (bitwise &, no short-circuit): the old
    # `and` chain skipped the password compare on a username miss — a
    # measurable timing oracle for username validity (2026-08-12 audit
    # SEC-9). The `configured` gate may short-circuit: configured-vs-not is
    # deployment state, not a per-request secret.
    user_ok = _const_eq(supplied_user, expected_user)
    pass_ok = _const_eq(supplied_password, expected_password)
    return configured and (user_ok & pass_ok)


async def enforce_control_plane_auth(request: Request, call_next):
    """Leave only liveness public; require auth and intent for everything else."""
    # Authentication exemptions must use the ASGI route path, not
    # ``request.url.path``: Starlette builds the latter from the untrusted Host
    # header, so a malformed authority can disguise a protected request as the
    # public liveness path (GHSA-86qp-5c8j-p5mr).
    if request.scope["path"] == LIVE_PATH:
        return await call_next(request)
    if not _valid_basic_auth(request.headers.get("authorization", "")):
        return JSONResponse(
            {"detail": "authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="DevCake"'},
        )
    if (
        request.method in MUTATING_METHODS
        and request.headers.get("x-devcake-request") != "1"
    ):
        return JSONResponse({"detail": "missing request intent header"}, status_code=403)
    token = REQUEST_ACTOR.set(request_actor(request))
    try:
        return await call_next(request)
    finally:
        REQUEST_ACTOR.reset(token)
