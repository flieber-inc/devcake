"""ports/forge.py declares — in bold — "adapters must never leak httpx
exceptions upward". The 2026-08-12 audit (F7) found all three forge
adapters violating it (raw ConnectError/ReadTimeout escaped `_req`) while
both PMO adapters wrapped correctly. The wire call now goes through ONE
chokepoint (adapters/http.forge_request); this contract pins it for every
adapter class at once, so the next forge tenant inherits the guard."""

import asyncio

import httpx
import pytest

from devcake.adapters.gitea.adapter import GiteaForge
from devcake.adapters.github.adapter import GitHubForge
from devcake.adapters.gitlab.adapter import GitLabForge
from devcake.ports.forge import ForgeError

FORGES = [
    (GitHubForge, "https://github.com/o/r"),
    (GitLabForge, "https://gitlab.com/o/r"),
    (GiteaForge, "http://gitea:3000/o/r"),
]


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _forge(cls, url, handler):
    return cls(url, "tok", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("cls,url", FORGES)
def test_network_failure_surfaces_as_forge_error_status_none(cls, url):
    def handler(req):
        raise httpx.ConnectError("connection refused", request=req)

    forge = _forge(cls, url, handler)
    with pytest.raises(ForgeError) as exc:
        run(forge.pr_state(1))
    assert exc.value.status is None, "network failures carry no HTTP status"
    assert "network" in str(exc.value)


@pytest.mark.parametrize("cls,url", FORGES)
def test_health_probe_reports_network_failure_as_transient(cls, url):
    """A probe must NEVER raise to the admin UI, and status=None is not a
    credential indictment — transient, retried."""
    def handler(req):
        raise httpx.ReadTimeout("timed out", request=req)

    forge = _forge(cls, url, handler)
    health = run(forge.health_probe())
    assert health.ok is False
    assert health.transient is True


@pytest.mark.parametrize("cls,url", FORGES)
def test_http_4xx_carries_its_status(cls, url):
    def handler(req):
        return httpx.Response(400, text="bad request")

    forge = _forge(cls, url, handler)
    with pytest.raises(ForgeError) as exc:
        run(forge.pr_state(1))
    assert exc.value.status == 400


def test_toolkit_imports_no_vendor_adapter():
    """Import-direction half of the _toolkit contract (the domain half is
    the domain→adapters ban in test_structure_guards): the shared helpers
    must never grow a vendor dependency."""
    import ast
    from pathlib import Path

    import devcake.adapters._toolkit as toolkit
    tree = ast.parse(Path(toolkit.__file__).read_text())
    vendors = {"linear", "gitea", "gitea_issues", "github", "gitlab",
               "github_issues", "gitlab_issues", "redis",
               "dagu", "files"}
    offenders = []
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        for m in mods:
            if set(m.split(".")) & vendors:
                offenders.append(m)
    assert not offenders, f"_toolkit must stay vendor-free: {offenders}"


# ── Direct forge_request seam (adapters/http) ────────────────────────────────

def test_forge_request_maps_network_to_forge_error_status_none():
    """The chokepoint itself — not only each adapter's _req — must convert
    httpx network failures to ForgeError(status=None)."""
    from devcake.adapters.http import forge_request

    def handler(req):
        raise httpx.ConnectError("connection refused", request=req)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ForgeError) as exc:
            run(forge_request(client, "GET", "http://example/x",
                              path_label="/x"))
        assert exc.value.status is None
        assert "network" in str(exc.value)
    finally:
        run(client.aclose())


def test_forge_request_maps_http_4xx_preserving_status():
    from devcake.adapters.http import forge_request

    def handler(req):
        return httpx.Response(403, text="forbidden")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ForgeError) as exc:
            run(forge_request(client, "GET", "http://example/x",
                              path_label="/x"))
        assert exc.value.status == 403
        assert "403" in str(exc.value)
    finally:
        run(client.aclose())


def test_forge_request_returns_json_and_raw_bytes():
    from devcake.adapters.http import forge_request

    def handler(req):
        if req.url.path.endswith("/raw"):
            return httpx.Response(200, content=b"blob-bytes")
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        body = run(forge_request(client, "GET", "http://example/j",
                                 path_label="/j"))
        assert body == {"ok": True}
        raw = run(forge_request(client, "GET", "http://example/raw",
                                path_label="/raw", raw=True))
        assert raw == b"blob-bytes"
    finally:
        run(client.aclose())
