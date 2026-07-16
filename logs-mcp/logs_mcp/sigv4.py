"""Minimal AWS Signature Version 4 signer — stdlib only.

Just enough for the CloudWatch Logs JSON API (POST to the service endpoint,
no query string): static keys + optional STS session token. Deliberately NOT
the full credential-provider chain (no IMDS/SSO/profiles) — credentials
arrive via DevType.secret_env. Verified against the published AWS SigV4
test-suite 'get-vanilla' vector and botocore-generated reference signatures
(test_logs_mcp.py).
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import urlsplit

_ALGO = "AWS4-HMAC-SHA256"


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign_headers(*, method: str, url: str, region: str, service: str,
                 headers: dict[str, str], body: bytes,
                 access_key: str, secret_key: str,
                 session_token: str | None = None,
                 now: datetime | None = None) -> dict[str, str]:
    """Return `headers` plus X-Amz-Date, optional X-Amz-Security-Token, and
    the SigV4 Authorization header. The host header is signed (derived from
    `url`) but not returned — the HTTP client sets it identically."""
    ts = now if now is not None else datetime.now(timezone.utc)
    amz_date = ts.strftime("%Y%m%dT%H%M%SZ")
    date = ts.strftime("%Y%m%d")
    parts = urlsplit(url)

    out = dict(headers)
    out["X-Amz-Date"] = amz_date
    if session_token:
        out["X-Amz-Security-Token"] = session_token

    to_sign = {k.lower(): v.strip() for k, v in out.items()}
    to_sign["host"] = parts.netloc
    signed_names = ";".join(sorted(to_sign))
    canonical_headers = "".join(f"{k}:{to_sign[k]}\n" for k in sorted(to_sign))
    canonical_request = "\n".join([
        method,
        parts.path or "/",
        "",                                   # no query string (JSON APIs)
        canonical_headers,
        signed_names,
        hashlib.sha256(body).hexdigest(),
    ])

    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        _ALGO, amz_date, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    k = _hmac(f"AWS4{secret_key}".encode(), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    k = _hmac(k, "aws4_request")
    signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()

    out["Authorization"] = (f"{_ALGO} Credential={access_key}/{scope}, "
                            f"SignedHeaders={signed_names}, "
                            f"Signature={signature}")
    return out
