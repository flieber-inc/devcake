"""ClaimsNotebooks — the forge write seam for `.claims/` (PLAN_MEMORY §5).

The app writes with its own credentials, never a mission Dev token.
Callers never touch note bodies. Touch only paths under `.claims/`.
"""

from __future__ import annotations

from typing import Protocol


class ClaimsNotebooks(Protocol):
    async def list_json_names(self, card: str) -> list[str] | None:
        """`*.json` filenames under `.claims/` (no path prefix).
        `[]` only when checkout succeeds and `.claims/` has no JSON.
        None if unlistable (missing card, clone/ls-remote failure)."""
        ...

    async def snapshot(self, card: str) -> dict | None:
        """`{json_names: [...], has_readme: bool}` in ONE checkout — the
        append path's listing + README presence probe. None if unlistable
        (including clone failure); definite-empty only after a successful
        checkout."""
        ...

    async def list_claim_meta(self, card: str) -> list[dict] | None:
        """`{id, source_instance, source_pmo_id}` for each claim file.
        Never includes finding/evidence/scope. None if unlistable."""
        ...

    async def has_readme(self, card: str) -> bool | None:
        """True if `.claims/README.md` exists. None if unlistable."""
        ...

    async def commit(self, card: str, *, creates: dict[str, str],
                     deletes: list[str], message: str) -> None:
        """Create/update text files and delete paths, all under `.claims/`.
        `creates` keys are paths relative to the notebook root (must start
        with `.claims/`). Raises on write failure."""
        ...

    def can_write(self, card: str) -> bool:
        """False for unknown / `reference_only` cards (skip + audit)."""
        ...
