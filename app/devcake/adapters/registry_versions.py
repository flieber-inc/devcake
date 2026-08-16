"""Look up the remote latest harness CLI version.

Operator-asked only (Use latest / Check for a newer version). Not on
editor open. Egress to npm and x.ai — named in docs/14.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..house_pins import PACKAGE_IDS

GROK_STABLE = "https://x.ai/cli/stable"
TIMEOUT_SECS = 8.0


class RegistryVersionSource:
    def latest(self, template: str) -> str:
        if template == "grok-build":
            return _http_text(GROK_STABLE).splitlines()[0].strip()
        pkg = PACKAGE_IDS.get(template)
        if pkg == "x.ai/cli":
            return _http_text(GROK_STABLE).splitlines()[0].strip()
        if not pkg:
            raise ValueError(f"no remote latest source for {template}")
        body = json.loads(_http_text(f"https://registry.npmjs.org/{pkg}/latest"))
        ver = str(body.get("version") or "").strip()
        if not ver:
            raise ValueError(f"npm returned no version for {pkg}")
        return ver


def _http_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "devcake"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECS) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValueError(f"latest lookup failed: {exc}") from exc
