"""`devcake mcp` end to end through the MCP SDK client (ADR-0041).

A stdlib stub stands in for the admin proxy: it serves the app's real OpenAPI
document, checks basic auth, the intent header and the actor label, and
answers two operations. The CLI verb runs as the stdio subprocess an agent
would launch. Skipped where the optional extra is not installed (the app
image); run it locally with `devcake-cli[mcp]` present.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")
anyio = pytest.importorskip("anyio")

_CLI = next((p for p in (Path(__file__).resolve().parents[2] / "cli", Path("/srv/cli")) if p.is_dir()), None)
assert _CLI is not None


def _openapi() -> dict:
    try:
        from devcake.api.main import app
        return app.openapi()
    except Exception:  # noqa: BLE001 — outside the app image the captured document serves
        path = os.environ.get("DEVCAKE_MCP_TEST_OPENAPI")
        if not path:
            pytest.skip("no app import and no DEVCAKE_MCP_TEST_OPENAPI document")
        return json.loads(Path(path).read_text())


class Stub(BaseHTTPRequestHandler):
    doc: dict = {}
    seen: list[dict] = []
    auth = "Basic " + base64.b64encode(b"op:pw").decode()

    def log_message(self, *a):  # quiet
        pass

    def _reply(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        Stub.seen.append({"method": self.command, "path": self.path,
                          "actor": self.headers.get("X-DevCake-Actor"),
                          "intent": self.headers.get("X-DevCake-Request")})
        if self.headers.get("Authorization") != Stub.auth:
            return self._reply(401, {"detail": "authentication required"})
        if self.command != "GET" and self.headers.get("X-DevCake-Request") != "1":
            return self._reply(403, {"detail": "missing request intent header"})
        if self.path == "/api/v1/openapi.json":
            return self._reply(200, Stub.doc)
        if self.path == "/api/v1/health":
            return self._reply(200, {"intake_paused": True, "active_runs": 0})
        if self.path.startswith("/api/v1/cron/") and self.path.endswith("/run"):
            return self._reply(409, {"detail": "intake is paused on board — no ticket created"})
        return self._reply(404, {"detail": "not found"})

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle


@pytest.fixture
def stub():
    Stub.doc = _openapi()
    Stub.seen = []
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def checkout(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "docker-bake.hcl").write_text('group "default" {}\n')
    (tmp_path / ".env").write_text("ADMIN_USER=op\nADMIN_PASSWORD=pw\n")
    return tmp_path


async def _session(stub_url: str, root: Path, *args: str):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "devcake_cli", "mcp", *args], cwd=str(root),
        env={**os.environ, "PYTHONPATH": str(_CLI), "DEVCAKE_ADMIN_URL": stub_url})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            health = await session.call_tool("health", {})
            cron = None
            if "run_cron" in {t.name for t in tools}:
                cron = await session.call_tool("run_cron", {"job_id": "memory-curator"})
            return tools, health, cron


def test_read_write_server_lists_derived_tools_and_calls_through(stub, checkout):
    tools, health, cron = anyio.run(_session, stub, checkout)
    names = {t.name for t in tools}
    assert {"health", "activity_now", "list_runs", "run_cron", "put_config"} <= names
    assert not {"export_settings", "put_secret", "clear_runs", "export_runs_csv"} & names
    assert all(t.description for t in tools)
    assert not health.is_error
    assert json.loads(health.content[0].text)["intake_paused"] is True
    assert cron is not None and cron.is_error and "HTTP 409" in cron.content[0].text
    calls = [s for s in Stub.seen if s["path"] != "/api/v1/openapi.json"]
    assert all(s["actor"] == "mcp" for s in calls)
    post = next(s for s in calls if s["method"] == "POST")
    assert post["intent"] == "1"


def test_read_only_server_exposes_get_only(stub, checkout):
    tools, health, cron = anyio.run(_session, stub, checkout, "--read-only")
    assert tools and cron is None
    assert "run_cron" not in {t.name for t in tools} and "health" in {t.name for t in tools}
    assert not health.is_error
