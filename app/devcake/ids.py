"""Human-readable run ids: {mission_key}-{seq}-{TYPE}-{6-char suffix} (docs/02 §7).

The id is simultaneously the Dagu dagRunId, the container name suffix
(dev-{run_id}), the Redis reply-stream key, and the trace correlation id —
so it must satisfy Dagu's ^[-a-zA-Z0-9_]+$ / ≤64 chars (verified) and
Docker's container-name charset.
"""

import re
import secrets

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32, ULID-style


def _suffix(n: int = 6) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def make_run_id(mission_key: str, seq: int, mission_type: str) -> str:
    key = _SAFE.sub("-", mission_key).strip("-")[:24]
    mtype = _SAFE.sub("", mission_type.upper())[:12]
    run_id = f"{key}-{seq}-{mtype}-{_suffix()}"
    assert len(run_id) <= 64
    return run_id
