"""Path-component hygiene + resolve confinement under a trusted base.

**Parts-not-base rule:** tainted values ride as ``*parts``; ``base`` must be
a constant (trusted root). Never build ``base`` from user input — that is
what left template save/delete open to CodeQL ``py/path-injection`` after
profiles/secrets cleared (CAKE-140).

Used by the secret-store builders, profiles, prompt templates, and Dev Type
path sinks so untrusted path segments cannot escape a jail directory even
when a higher-layer regex gate is skipped.
"""

from __future__ import annotations

import os
from pathlib import Path


def confined(base: Path, *parts: str) -> Path:
    """Join ``parts`` under ``base``; raise ``ValueError`` on unsafe segments
    or any result that escapes ``base`` after normalize / resolve.

    **Parts-not-base:** tainted values ride as ``*parts``; ``base`` must be a
    constant (trusted root). Each part must be a non-empty single path
    component (no ``/``, ``\\``, NUL, ``.``, or ``..``). A normalize-then-
    prefix check then ``resolve()`` + ``relative_to`` hold the result under
    ``base`` (the resolve belt follows symlinks). Whitespace inside a
    component is allowed — profile names may contain spaces.
    """
    for p in parts:
        if not p or p in (".", "..") or "/" in p or "\\" in p or "\x00" in p:
            raise ValueError(f"unsafe path component {p!r}")
    # CodeQL-recognized barrier (normalize-then-prefix) before resolve sink.
    norm = os.path.normpath(str(base.joinpath(*parts)))
    base_norm = os.path.normpath(str(base))
    if not norm.startswith(base_norm + os.sep):
        raise ValueError(f"path {norm!r} escapes base {base_norm!r}")
    path = base.joinpath(*parts).resolve()
    path.relative_to(base.resolve())  # raises ValueError on escape
    return path
