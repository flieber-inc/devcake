"""OpenObserve Basic auth for Dev OTLP export (ISSUES #13).

Single source of truth — used by both mission runspecs and HELLO secrets so
the root-credential warning fires at most once per process.
"""

from __future__ import annotations

import base64
import logging
import os

log = logging.getLogger("devcake.oo_auth")
_OO_ROOT_WARNED = False


def oo_basic_auth() -> str:
    """Prefer OO_INGEST_* over root credentials so a compromised Dev does not
    own the whole OpenObserve tenant."""
    global _OO_ROOT_WARNED
    ingest_email = os.environ.get("OO_INGEST_EMAIL", "")
    ingest_password = os.environ.get("OO_INGEST_PASSWORD", "")
    if ingest_email and ingest_password:
        return base64.b64encode(
            f"{ingest_email}:{ingest_password}".encode()).decode()
    email = os.environ.get("OO_ROOT_EMAIL", "")
    password = os.environ.get("OO_ROOT_PASSWORD", "")
    if not _OO_ROOT_WARNED:
        _OO_ROOT_WARNED = True
        log.warning(
            "OO_INGEST_EMAIL/OO_INGEST_PASSWORD unset — Dev runspecs receive "
            "OpenObserve ROOT credentials. Create an ingest-only OO user and "
            "set OO_INGEST_* (ISSUES #13).")
    return base64.b64encode(f"{email}:{password}".encode()).decode()
