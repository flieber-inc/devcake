"""Idempotent OpenObserve ingest-user provision (docs/12 §1, ISSUES #13).

The stack authenticates telemetry with ``OO_INGEST_*``; OpenObserve only
creates the root user from compose. This module creates/resyncs the ingest
service account so a filled ``.env`` + ``compose up`` is enough — no host-side
``scripts/provision_oo.py`` step for connectivity (that script remains for
dashboard + optional alerts).

Called fail-loud from app lifespan: OO is non-negotiable for DevCake.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

import httpx

from . import OO_ORG, OO_URL

log = logging.getLogger("devcake.telemetry.oo_provision")


class OoProvisionError(RuntimeError):
    """Ingest user missing/broken and could not be repaired."""


def _basic(email: str, password: str) -> str:
    return base64.b64encode(f"{email}:{password}".encode()).decode()


async def ensure_oo_ingest_user(
    *,
    base_url: str | None = None,
    org: str | None = None,
    root_email: str | None = None,
    root_password: str | None = None,
    ingest_email: str | None = None,
    ingest_password: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 10.0,
) -> str:
    """Ensure the OO ingest service account exists and matches credentials.

    Returns one of ``"verified"``, ``"created"``, ``"password_resynced"``.
    Raises ``OoProvisionError`` on any unrecoverable failure.
    """
    base = (base_url if base_url is not None else OO_URL).rstrip("/")
    org_name = org if org is not None else OO_ORG
    root_e = root_email if root_email is not None else os.environ.get("OO_ROOT_EMAIL", "")
    root_p = root_password if root_password is not None else os.environ.get("OO_ROOT_PASSWORD", "")
    email = ingest_email if ingest_email is not None else os.environ.get("OO_INGEST_EMAIL", "")
    password = (ingest_password if ingest_password is not None
                else os.environ.get("OO_INGEST_PASSWORD", ""))

    if not (email or "").strip() or not (password or "").strip():
        raise OoProvisionError(
            "OO_INGEST_EMAIL / OO_INGEST_PASSWORD must be set "
            "(the OO service account — ISSUES #13)")
    if not (root_e or "").strip() or not (root_p or "").strip():
        raise OoProvisionError(
            "OO_ROOT_EMAIL / OO_ROOT_PASSWORD must be set to provision the "
            "ingest service account")

    root_auth = _basic(root_e, root_p)
    ingest_auth = _basic(email, password)
    api = f"{base}/api/{org_name}"

    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:

        async def req(method: str, path: str, *, auth: str,
                      body: dict | None = None) -> tuple[int, Any]:
            r = await client.request(
                method, f"{api}{path}",
                headers={"Authorization": f"Basic {auth}",
                         "Content-Type": "application/json"},
                json=body)
            try:
                data: Any = r.json()
            except Exception:  # noqa: BLE001 — OO error bodies are not always JSON
                data = r.text[:400]
            return r.status_code, data

        async def creds_work() -> bool:
            code, _ = await req("GET", "/streams?type=logs", auth=ingest_auth)
            return code == 200

        code, users_body = await req("GET", "/users", auth=root_auth)
        if code != 200:
            raise OoProvisionError(
                f"cannot list OO users (root creds wrong? OO down?): "
                f"HTTP {code} {users_body!r}")

        data = users_body.get("data") if isinstance(users_body, dict) else None
        exists = any(
            isinstance(u, dict) and u.get("email") == email
            for u in (data or []))

        if exists:
            if await creds_work():
                log.info("oo ingest user %s exists, credentials verified", email)
                return "verified"
            code, out = await req(
                "PUT", f"/users/{email}", auth=root_auth,
                body={"new_password": password, "change_password": True})
            if code != 200 or not await creds_work():
                raise OoProvisionError(
                    f"ingest user {email} exists but password does not "
                    f"authenticate and resync failed: HTTP {code} {out!r}")
            log.info("oo ingest user %s password resynced from env", email)
            return "password_resynced"

        code, out = await req(
            "POST", "/users", auth=root_auth,
            body={"email": email, "password": password,
                  "first_name": "DevCake", "last_name": "Ingest",
                  "role": "service_account"})
        if code not in (200, 201):
            raise OoProvisionError(
                f"creating ingest user failed: HTTP {code} {out!r}")
        if not await creds_work():
            raise OoProvisionError(
                f"ingest user created but credentials do not authenticate — "
                f"check OO_URL/OO_ORG ({api})")
        log.info("oo ingest user %s created", email)
        return "created"


async def ensure_oo_ingest_user_at_boot(
    *,
    attempts: int = 30,
    delay_s: float = 2.0,
) -> str:
    """Retrying wrapper for lifespan: OO may still be starting after compose.

    Fail-loud after retries so the app container restarts until OO is ready
    and the ingest account is guaranteed (OO non-negotiable).
    """
    import asyncio

    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return await ensure_oo_ingest_user()
        except (OoProvisionError, httpx.HTTPError, OSError) as e:
            last = e
            log.warning("oo ingest provision attempt %s/%s failed: %s",
                        i, attempts, e)
            if i < attempts:
                await asyncio.sleep(delay_s)
    raise OoProvisionError(
        f"OO ingest auto-provision failed after {attempts} attempts: {last}")
