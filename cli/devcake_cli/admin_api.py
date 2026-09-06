"""The one loopback admin-API client every CLI verb shares (ADR-0038, ADR-0041).

The admin proxy is pinned to loopback in docker-compose.yml (docs/14); the
credentials come from the checkout's `.env`; every mutation carries the
intent header; the optional actor label rides `X-DevCake-Actor` so the
app's audit rows say who acted. Never routed through an `http_proxy`.
"""
from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .setup import _basic_auth_header, _load_admin_auth

# Loopback by default (docs/14). DEVCAKE_ADMIN_URL exists for an operator
# reaching the host through an SSH tunnel on another port, and for tests;
# it is never a licence to publish the admin port.
ADMIN_URL = os.environ.get("DEVCAKE_ADMIN_URL", "http://127.0.0.1:8080")


class AdminUnreachable(RuntimeError):
    """The stack did not answer, or `.env` carries no admin credentials."""


def _opener() -> urllib.request.OpenerDirector:
    # never route a loopback call — and the admin password — through an
    # http_proxy from the environment
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(root: Path, method: str, path: str, *, query: dict | None = None,
            body: Any = None, actor: str | None = None,
            timeout: float = 30.0) -> tuple[int, Any]:
    """(status, decoded JSON) for one call to the loopback admin API.

    HTTP errors come back as their status and decoded body (the app's
    `{"detail": …}`), never as an exception; only an unreachable stack or
    missing credentials raise `AdminUnreachable`, with the same wording the
    status verb prints.
    """
    try:
        user, password = _load_admin_auth(root)
    except (RuntimeError, OSError) as exc:
        raise AdminUnreachable(str(exc)) from exc
    url = f"{ADMIN_URL}{path}"
    if query:
        clean = {k: v for k, v in query.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean, doseq=True)
    headers = {"Authorization": _basic_auth_header(user, password),
               "Accept": "application/json"}
    if method.upper() != "GET":
        headers["X-DevCake-Request"] = "1"
    if actor:
        headers["X-DevCake-Actor"] = actor
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with _opener().open(req, timeout=timeout) as resp:  # noqa: S310 — loopback, fixed scheme
            return resp.status, _decode(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())
    except (OSError, ValueError, http.client.HTTPException) as exc:
        detail = getattr(exc, "reason", None) or exc
        raise AdminUnreachable(
            f"the admin proxy at {ADMIN_URL} could not be reached "
            f"({exc.__class__.__name__}: {detail}) — stack down?") from exc


def _decode(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return {"detail": text[:2000]}
