"""Human-readable run ids: {INSTANCE}-{key}-{seq}-{TYPE}-{6-char suffix}
(docs/02 §7). The uppercased PMO-instance prefix (schema v3) keeps ids
collision-free across instances.

The id is simultaneously the Dagu dagRunId, the container name suffix
(dev-{run_id}), the Redis reply-stream key, and the trace correlation id —
so it must satisfy Dagu's ^[-a-zA-Z0-9_]+$ / ≤64 chars (verified) and
Docker's container-name charset.
"""

import re
import secrets

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32, ULID-style


class RunIdOverflow(ValueError):
    """The composed run id would exceed the 64-char Dagu budget — reachable
    only through an inflated seq (e.g. a forged `9…9_EXECUTE.md` feed
    marker), so callers gate the one mission instead of crashing the cycle."""


def _suffix(n: int = 6) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def run_id_instance_prefix(instance: str) -> str:
    """Scrubbed uppercased instance segment embedded in make_run_id (≤12).

    Instance identities may be longer (config: ≤39, underscore allowed);
    run ids keep a 12-char prefix so the 64-char Dagu budget holds.
    AppConfig refuses two live names whose prefixes collide.
    """
    return _SAFE.sub("", instance.upper())[:12]


def make_run_id(instance: str, mission_key: str, seq: int, mission_type: str) -> str:
    """{INSTANCE}-{key}-{seq}-{TYPE}-{suffix}. The uppercased PMO-instance
    prefix (schema v3) keeps run ids — and therefore ACL users, container
    names, and reply streams — collision-free across instances. The
    instance segment is scrubbed-upper truncated to 12 chars (full identity
    may be longer; config enforces prefix uniqueness). HELLO/OAUTH runs
    pass the fixed pseudo-instance "sys" (instance-less by design; they
    finalize inside RunManager)."""
    inst = run_id_instance_prefix(instance)
    key = _SAFE.sub("-", mission_key).strip("-")[:24]
    mtype = _SAFE.sub("", mission_type.upper())[:12]
    run_id = f"{inst}-{key}-{seq}-{mtype}-{_suffix()}"
    if len(run_id) > 64:
        raise RunIdOverflow(
            f"run id would be {len(run_id)} chars (>64 Dagu budget): "
            f"instance={instance!r} key={mission_key!r} seq={seq}")
    return run_id
