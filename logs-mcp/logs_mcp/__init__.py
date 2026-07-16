"""devcake-logs-mcp: agent-facing log access — standalone stdio MCP server.

Layering (keep it): core.py is stdlib-only (the LogBackend seam, row shape,
formatting); datadog.py/cloudwatch.py/sigv4.py add only httpx; server.py is
the ONLY module importing the `mcp` SDK, so the test suite runs without it.
devcake bakes this package into every Dev image (pip install --no-deps,
images/Dockerfile); registration is per Dev Type via mcp_setup_commands
(docs/08 §7)."""
