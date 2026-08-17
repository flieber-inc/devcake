"""Host-side Grok CLI OAuth session refresh (concierge pattern).

Refreshes `/data/secrets/{dev}/grok-auth.json` on the control plane before
runspec inject. Mission Devs receive the full CLI file (including
``refresh_token``) so mid-run Grok refresh still works; they never write
tokens back to the host (docs/14).
"""

from __future__ import annotations

import base64
import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..ports.oidc_token import (OidcRefreshRevoked, OidcTokenError,
                                OidcTokenPort, TokenRefreshResult)

log = logging.getLogger("devcake.grok_oauth")

# Refresh slightly early so an access token never expires mid-boot.
EXPIRY_SLACK_S = 120.0
DEFAULT_TOKEN_URL = "https://auth.x.ai/oauth2/token"
GROK_AUTH_FILENAME = "grok-auth.json"


class CredentialRefreshError(Exception):
    """Public, non-secret reason for runspec.error (fail closed at inject)."""


@dataclass
class GrokAuthSession:
    """One Grok CLI auth.json document (nested scope → entry)."""
    root: dict
    scope_key: str

    @property
    def entry(self) -> dict:
        return self.root[self.scope_key]


def parse_grok_auth(raw: str) -> GrokAuthSession:
    """Parse Grok CLI auth.json. Raises CredentialRefreshError if unusable."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise CredentialRefreshError(
            "grok OAuth file is corrupt — reconnect via admin OAuth") from e
    if not isinstance(data, dict) or not data:
        raise CredentialRefreshError(
            "grok OAuth file is empty — reconnect via admin OAuth")
    # Prefer the first scope entry that looks like an OIDC session.
    scope_key = None
    for k, v in data.items():
        if isinstance(v, dict) and (v.get("key") or v.get("access_token")):
            scope_key = k
            break
    if scope_key is None:
        raise CredentialRefreshError(
            "grok OAuth file has no access token — reconnect via admin OAuth")
    return GrokAuthSession(root=data, scope_key=scope_key)


def _jwt_exp(access_token: str) -> float | None:
    if not access_token or access_token.count(".") != 2:
        return None
    try:
        payload = access_token.split(".")[1]
        pad = "=" * ((4 - len(payload) % 4) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload + pad))
        exp = claims.get("exp")
        return float(exp) if exp is not None else None
    except Exception:  # noqa: BLE001 — malformed JWT ⇒ no exp signal
        return None


def _parse_expires_at(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # Grok may emit nanoseconds; fromisoformat accepts ≤6 fractional digits.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, rest = text.split(".", 1)
        frac = ""
        tz = ""
        for i, ch in enumerate(rest):
            if ch.isdigit():
                frac += ch
            else:
                tz = rest[i:]
                break
        frac = (frac + "000000")[:6]
        text = f"{head}.{frac}{tz}"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def access_expiry_unix(entry: dict) -> float | None:
    """Earliest known expiry from expires_at and/or JWT exp."""
    candidates: list[float] = []
    ea = _parse_expires_at(entry.get("expires_at"))
    if ea is not None:
        candidates.append(ea)
    tok = entry.get("key") or entry.get("access_token") or ""
    jwt_exp = _jwt_exp(str(tok))
    if jwt_exp is not None:
        candidates.append(jwt_exp)
    return min(candidates) if candidates else None


def needs_refresh(session: GrokAuthSession, *, now: float | None = None,
                  slack_s: float = EXPIRY_SLACK_S) -> bool:
    """True when access is missing/expired/near-expiry, or expiry unknown
    but a refresh_token exists (force refresh rather than inject blind)."""
    entry = session.entry
    if not entry.get("refresh_token"):
        return False
    exp = access_expiry_unix(entry)
    if exp is None:
        return True
    return float(now if now is not None else time.time()) + slack_s >= exp


def apply_refresh(session: GrokAuthSession,
                  result: TokenRefreshResult,
                  *, now: float | None = None) -> GrokAuthSession:
    """Map token-endpoint fields onto Grok CLI entry shape (key, expires_at)."""
    entry = dict(session.entry)
    entry["key"] = result.access_token
    if result.refresh_token:
        entry["refresh_token"] = result.refresh_token
    ts = float(now if now is not None else time.time())
    if result.expires_in is not None:
        exp_unix = ts + float(result.expires_in)
    else:
        exp_unix = _jwt_exp(result.access_token) or (ts + 3600.0)
    entry["expires_at"] = datetime.fromtimestamp(
        exp_unix, timezone.utc).isoformat().replace("+00:00", "Z")
    root = dict(session.root)
    root[session.scope_key] = entry
    return GrokAuthSession(root=root, scope_key=session.scope_key)


def serialize_grok_auth(session: GrokAuthSession) -> str:
    return json.dumps(session.root, indent=2) + "\n"


def ensure_fresh_for_inject(
    raw: str,
    *,
    token_port: OidcTokenPort,
    write_full: Callable[[str], None],
    lock_path: Path,
    now: float | None = None,
    slack_s: float = EXPIRY_SLACK_S,
    token_url: str = DEFAULT_TOKEN_URL,
    timeout: float = 15.0,
    reread: Callable[[], str] | None = None,
) -> str:
    """Refresh host file if needed; return full CLI JSON for Dev inject.

    Raises CredentialRefreshError on parse/refresh failure (fail closed).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Ensure lock file exists for flock.
    with open(lock_path, "a+", encoding="utf-8") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            current = reread() if reread is not None else raw
            session = parse_grok_auth(current)
            if not needs_refresh(session, now=now, slack_s=slack_s):
                return serialize_grok_auth(session)
            entry = session.entry
            refresh = entry.get("refresh_token")
            if not refresh:
                raise CredentialRefreshError(
                    "grok OAuth has no refresh_token — reconnect via admin OAuth")
            client_id = (entry.get("oidc_client_id")
                         or entry.get("client_id"))
            try:
                result = token_port.refresh(
                    refresh_token=str(refresh),
                    client_id=str(client_id) if client_id else None,
                    token_url=token_url,
                    timeout=timeout,
                )
            except OidcRefreshRevoked as e:
                raise CredentialRefreshError(
                    "grok OAuth refresh revoked — reconnect via admin OAuth"
                ) from e
            except OidcTokenError as e:
                raise CredentialRefreshError(
                    "grok OAuth refresh failed — reconnect via admin OAuth "
                    "or retry shortly"
                ) from e
            except Exception as e:  # noqa: BLE001 — unexpected adapter bugs must not inject stale sessions under fail-closed
                log.exception("grok OAuth refresh raised unexpectedly")
                raise CredentialRefreshError(
                    "grok OAuth refresh failed — reconnect via admin OAuth"
                ) from e
            if not result.access_token:
                raise CredentialRefreshError(
                    "grok OAuth refresh returned no access token — "
                    "reconnect via admin OAuth")
            updated = apply_refresh(session, result, now=now)
            text = serialize_grok_auth(updated)
            write_full(text)
            log.info("grok OAuth host refresh ok for scope %s",
                     session.scope_key[:48])
            return text
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
