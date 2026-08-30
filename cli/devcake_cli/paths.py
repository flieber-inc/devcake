"""Checkout-root discovery for the host CLI."""

from __future__ import annotations

from pathlib import Path


def find_checkout_root(start: Path | None = None) -> Path | None:
    """Walk upward for ``docker-compose.yml`` + ``docker-bake.hcl``.

    Returns ``None`` when no DevCake checkout layout is found.
    """
    cur = (start or Path.cwd()).resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / "docker-compose.yml").is_file() and (
            candidate / "docker-bake.hcl"
        ).is_file():
            return candidate
    return None


def require_checkout_root(start: Path | None = None) -> Path:
    root = find_checkout_root(start)
    if root is None:
        raise FileNotFoundError(
            "not a DevCake checkout (need docker-compose.yml + docker-bake.hcl); "
            "cd to the repo root or re-clone"
        )
    return root
