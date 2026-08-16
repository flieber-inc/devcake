"""ReceiptStore — row-level drift-probe receipts on /data.

App reads. The host bake verb writes. Missing/failing receipts do not
change launch in Slice 1 (fail-open).
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol


class ReceiptStore(Protocol):
    def get(self, *, digest: str, template: str,
            cli_version: str) -> Mapping[str, Any] | None: ...
