"""Probe-row cause: aim | stub | dialect | auth (docs/08 adaptor plan §2a).

Bakery-only. Not a DEV_* class. Attribution uses the stub journal plus the
matrix triple — not fault() internals and not stub URLs inside classify.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# Wire paths the stub actually serves. A hit on another path is a stub miss
# (wrong protocol), not an aim miss.
_TAPE_PATHS = (
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/responses",
    "/v1/models",
)


def attribute(
    *,
    row: str,
    expected: Mapping[str, Any] | None,
    observed: Mapping[str, Any] | None,
    journal_hits: Iterable[Mapping[str, Any]],
    launched: bool = True,
) -> str:
    """Name why this classify row is not the matrix triple we wanted.

    Order: never launched → aim; no journal hit on this lane → aim;
    hit on a path the stub does not tape → stub; hit + 401 classified
    AUTH → auth; hit on the right lane, classify disagrees → dialect.
    """
    if not launched:
        return "aim"
    hits = [h for h in journal_hits
            if isinstance(h, Mapping) and h.get("scenario") == row]
    if not hits:
        return "aim"
    if not any(_on_tape(h.get("path")) for h in hits):
        return "stub"
    if (row == "http_401"
            and _triple(observed) == _AUTH
            and (expected is None or _triple(expected) == _AUTH)):
        return "auth"
    return "dialect"


_AUTH = {"exit": 12, "class": "DEV_AUTH", "reason": "terminal_error"}


def _on_tape(path: object) -> bool:
    text = str(path or "")
    return any(text == p or text.endswith(p) for p in _TAPE_PATHS)


def _triple(obs: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(obs, Mapping):
        return {}
    return {
        "exit": obs.get("exit"),
        "class": obs.get("class"),
        "reason": obs.get("reason"),
    }


def evidence(*, journal_hits: Iterable[Mapping[str, Any]], row: str) -> dict:
    hits = [h for h in journal_hits
            if isinstance(h, Mapping) and h.get("scenario") == row]
    paths = [str(h.get("path") or "") for h in hits]
    return {"journal_hits": len(hits), "paths": paths}
