"""Path-component hygiene + resolve confinement under a trusted base.

Used by the secret-store builders (and reusable by later callers such as
Dev Type move confinement) so untrusted path segments cannot escape a
jail directory even when a higher-layer regex gate is skipped.
"""

from __future__ import annotations

from pathlib import Path


def confined(base: Path, *parts: str) -> Path:
    """Join ``parts`` under ``base``; raise ``ValueError`` on unsafe segments
    or any result that escapes ``base`` after resolve.

    Each part must be a non-empty single path component (no ``/``, ``\\``,
    NUL, ``.``, or ``..``). Whitespace inside a component is allowed —
    profile names may contain spaces.
    """
    for p in parts:
        if not p or p in (".", "..") or "/" in p or "\\" in p or "\x00" in p:
            raise ValueError(f"unsafe path component {p!r}")
    path = base.joinpath(*parts).resolve()
    path.relative_to(base.resolve())  # raises ValueError on escape
    return path
