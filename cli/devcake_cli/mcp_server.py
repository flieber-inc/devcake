"""`devcake mcp` — the operator MCP server over stdio (ADR-0041).

Every tool is one admin-API operation, derived at startup from the app's own
OpenAPI document; calls go to the loopback admin proxy with the checkout's
credentials, the intent header on mutations and the `mcp` actor label. The
MCP SDK is an optional extra: the base CLI stays dependency-free.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import __version__, admin_api
from .mcp_catalog import ToolSpec, build_tools, render_call

ACTOR = "mcp"
OPENAPI_PATH = "/api/v1/openapi.json"
RESULT_CAP = 200_000          # characters of JSON handed back per call
INSTALL_HINT = ("devcake mcp needs the optional extra: "
                "uv tool install 'devcake-cli[mcp]' (or pip install 'devcake-cli[mcp]')")


def load_catalog(root: Path, *, read_only: bool) -> list[ToolSpec]:
    status, doc = admin_api.request(root, "GET", OPENAPI_PATH, actor=ACTOR)
    if status != 200 or not isinstance(doc, dict):
        detail = doc.get("detail") if isinstance(doc, dict) else doc
        raise admin_api.AdminUnreachable(
            f"the admin proxy answered HTTP {status} for the API description"
            + (" — check ADMIN_USER / ADMIN_PASSWORD in .env" if status in (401, 403) else "")
            + (f" ({detail})" if detail else ""))
    return build_tools(doc, read_only=read_only)


def call(root: Path, tool: ToolSpec, arguments: dict | None) -> tuple[bool, str]:
    """(is_error, text) for one tool call against the admin API."""
    try:
        method, path, query, body = render_call(tool, arguments)
    except ValueError as exc:
        return True, str(exc)
    try:
        status, payload = admin_api.request(root, method, path, query=query,
                                            body=body, actor=ACTOR, timeout=300)
    except admin_api.AdminUnreachable as exc:
        return True, str(exc)
    text = json.dumps(payload, indent=1, ensure_ascii=False) if payload is not None else ""
    if len(text) > RESULT_CAP:
        text = text[:RESULT_CAP] + "\n… (truncated)"
    if status >= 400:
        return True, f"HTTP {status}\n{text}"
    return False, text


def run_mcp(root: Path, *, read_only: bool) -> int:
    try:
        import anyio
        import mcp.types as types
        from mcp.server.lowlevel import Server
        from mcp.server.stdio import stdio_server
    except ImportError:
        sys.stderr.write(INSTALL_HINT + "\n")
        return 3
    try:
        tools = load_catalog(root, read_only=read_only)
    except admin_api.AdminUnreachable as exc:
        sys.stderr.write(f"devcake mcp: {exc}\n")
        return 3
    by_name = {t.name: t for t in tools}
    flavour = "read-only" if read_only else "read-write"

    async def on_list_tools(_ctx, _params):
        return types.ListToolsResult(tools=[types.Tool(**t.as_tool()) for t in tools])

    async def on_call_tool(_ctx, params):
        tool = by_name.get(params.name)
        if tool is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"unknown tool {params.name!r}")],
                isError=True)
        is_error, text = await anyio.to_thread.run_sync(call, root, tool, params.arguments)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                    isError=is_error)

    server = Server(
        "devcake", version=__version__,
        instructions=(f"DevCake operator tools ({flavour}): every tool is one call of the "
                      "deployment's admin API, described by the API itself. Read state "
                      "before changing it; mutations are audited as actor 'mcp'. Secret "
                      "values never cross these tools."),
        on_list_tools=on_list_tools, on_call_tool=on_call_tool)

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)
    return 0
