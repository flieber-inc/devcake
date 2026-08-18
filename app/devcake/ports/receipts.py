"""ReceiptStore — row-level drift-probe receipts on /data.

App reads. The host bake verb writes. Production adapter:
`adapters/files/receipts.py` (`FileReceiptStore`).

Staffing is **fail-closed** on this port: `require_staffed` (dispatch,
steward, OAuth — not hello) refuses launch unless `get` returns a matching
ok receipt for the app digest + template + effective CLI pin (docs/08,
docs/13). A miss (`None`) or non-ok / ungated row is a hard refuse, never a
silent continue.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol


class ReceiptStore(Protocol):
    def get(self, *, digest: str, template: str,
            cli_version: str) -> Mapping[str, Any] | None: ...
