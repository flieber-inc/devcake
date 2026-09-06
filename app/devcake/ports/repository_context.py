"""Cache capabilities needed to prepare a run's repository context."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ..config import AppConfig, DevType, PMOInstance


class RepositoryContextCache(Protocol):
    def needed_for(self, *, work_repo: str, mission_type: str,
                   instance: PMOInstance, blocker_entries: list[dict] | None,
                   dev_type: DevType, config: AppConfig) -> list[str]: ...

    def mirror_name_of(self, name: str) -> str: ...

    def eligible(self, name: str) -> bool: ...

    def has_last_good(self, name: str) -> bool: ...

    async def ensure_fresh(self, names: Iterable[str]) -> tuple[bool, dict[str, str]]: ...
