"""HTTP authentication and request-intent checks for the control-plane API."""

import base64
import binascii
import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse


LIVE_PATH = "/api/v1/health/live"
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def admin_credentials() -> tuple[str, str]:
    return os.environ.get("ADMIN_USER", ""), os.environ.get("ADMIN_PASSWORD", "")


def credentials_configured() -> bool:
    user, password = admin_credentials()
    return bool(user and password)


def _valid_basic_auth(value: str) -> bool:
    if not value.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(value[6:], validate=True).decode("utf-8")
        supplied_user, supplied_password = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    expected_user, expected_password = admin_credentials()
    return (
        bool(expected_user and expected_password)
        and secrets.compare_digest(supplied_user, expected_user)
        and secrets.compare_digest(supplied_password, expected_password)
    )


async def enforce_control_plane_auth(request: Request, call_next):
    """Leave only liveness public; require auth and intent for everything else."""
    if request.url.path == LIVE_PATH:
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
    return await call_next(request)
