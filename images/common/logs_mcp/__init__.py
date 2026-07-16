"""devcake-logs-mcp: agent-facing log access, baked into every Dev image.

Layering (keep it): core.py is stdlib-only (the LogBackend seam, row shape,
formatting) so app-test can import it; datadog.py adds httpx; server.py is
the ONLY module importing the `mcp` SDK. Registration is per Dev Type via
mcp_setup_commands (docs/08 §7)."""
