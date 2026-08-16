"""HarnessVersionSource — remote latest CLI version (operator-asked only)."""

from __future__ import annotations

from typing import Protocol


class HarnessVersionSource(Protocol):
    def latest(self, template: str) -> str: ...
