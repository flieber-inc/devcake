"""`devcake mcp` (ADR-0041): the tool catalogue is derived from the app's own
OpenAPI document — one tool per JSON operation, named by operation id,
described by the route's docstring; opted-out and non-JSON operations are
absent; read-only keeps GET. Asserted against the LIVE app description, so
a new route becomes a tool here without a line of catalogue code."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

_CLI_CANDIDATES = [Path(__file__).resolve().parents[2] / "cli", Path("/srv/cli")]
_cli = next((p for p in _CLI_CANDIDATES if p.is_dir()), None)
if _cli and str(_cli) not in sys.path:
    sys.path.insert(0, str(_cli))

from devcake_cli import admin_api, main as cli_main, mcp_catalog  # noqa: E402
from devcake_cli.mcp_catalog import build_tools, render_call  # noqa: E402


@pytest.fixture(scope="module")
def doc():
    from devcake.api.main import app
    return app.openapi()


def _ops(doc):
    return {(m.upper(), p): o for p, item in doc["paths"].items() for m, o in item.items()}


def test_every_json_operation_is_a_tool_and_opted_out_ones_are_not(doc):
    tools = {t.name: t for t in build_tools(doc, read_only=False)}
    ops = _ops(doc)
    never = {o["operationId"] for o in ops.values() if o.get("x-devcake-mcp") == "never"}
    assert {"export_settings", "import_settings", "put_secret", "clear_runs",
            "upload_credentials"} <= never
    assert not never & set(tools)
    assert "export_runs_csv" not in tools          # text/csv: excluded by its media type
    expected = {o["operationId"] for (m, p), o in ops.items()
                if o["operationId"] not in never and o["operationId"] != "export_runs_csv"}
    assert set(tools) == expected
    assert all(t.description for t in tools.values()), \
        [n for n, t in tools.items() if not t.description]
    assert len(tools) >= 60


def test_read_only_keeps_get_only(doc):
    ro = build_tools(doc, read_only=True)
    assert ro and all(t.method == "get" for t in ro)
    assert {"health", "activity_now", "list_runs", "get_config"} <= {t.name for t in ro}
    assert "run_cron" not in {t.name for t in ro}


def test_schemas_fold_path_query_and_body_and_inline_refs(doc):
    tools = {t.name: t for t in build_tools(doc, read_only=False)}
    rc = tools["run_cron"]
    assert rc.path_params == ("job_id",) and rc.input_schema["required"] == ["job_id"]
    lr = tools["list_runs"]
    assert "limit" in lr.query_params and "pmo_ref" in lr.query_params
    assert lr.input_schema["properties"]["limit"]["type"] == "integer"
    pc = tools["put_config"]
    assert pc.body and "body" in pc.input_schema["required"]
    assert "$ref" not in json.dumps([t.input_schema for t in tools.values()])
    assert all(t.input_schema.get("additionalProperties") is False for t in tools.values())


def test_render_call_builds_the_request(doc):
    tools = {t.name: t for t in build_tools(doc, read_only=False)}
    assert render_call(tools["run_cron"], {"job_id": "memory-curator"}) == \
        ("post", "/api/v1/cron/memory-curator/run", {}, None)
    assert render_call(tools["list_runs"], {"limit": 5, "pmo_ref": "board"}) == \
        ("get", "/api/v1/runs", {"limit": 5, "pmo_ref": "board"}, None)
    assert render_call(tools["put_config"], {"body": {"poll_interval_seconds": 30}}) == \
        ("put", "/api/v1/config", {}, {"poll_interval_seconds": 30})
    with pytest.raises(ValueError, match="missing path parameter"):
        render_call(tools["run_cron"], {})
    with pytest.raises(ValueError, match="empty path parameter"):
        render_call(tools["run_cron"], {"job_id": ""})


def test_path_arguments_cannot_change_a_tools_destination(doc):
    """A path value carrying separators is encoded, never spliced: the
    credential-upload route (opted out) cannot be reached through the
    clone tool's name argument, nor any other."""
    tools = {t.name: t for t in build_tools(doc, read_only=False)}
    _, path, _, _ = render_call(tools["clone_dev_type"], {"name": "demo/credentials#"})
    assert path == "/api/v1/dev-types/demo%2Fcredentials%23/clone"
    _, path, _, _ = render_call(tools["get_run"], {"run_id": "../../secrets?x=1"})
    assert path == "/api/v1/runs/..%2F..%2Fsecrets%3Fx%3D1"
    assert "#" not in path and "?" not in path


def test_admitted_write_tools_declare_their_fields(doc):
    """The catalogue teaches an agent what to send: every write tool with
    a body names its fields, except the settings patch, which is a
    described free-form document by design."""
    tools = {t.name: t for t in build_tools(doc, read_only=False)}
    untyped = sorted(n for n, t in tools.items() if t.body
                     and not (t.input_schema["properties"]["body"].get("properties")
                              or t.input_schema["properties"]["body"].get("additionalProperties") not in (None, True)))
    assert untyped == ["put_config"], untyped
    assert "description" in tools["put_config"].input_schema["properties"]["body"]
    tmpl = tools["put_prompt_template"].input_schema["properties"]["body"]
    assert tmpl["required"] == ["template"] and "template" in tmpl["properties"]
    skill = tools["create_skill"].input_schema["properties"]["body"]
    assert {"name", "description", "body", "overwrite"} <= set(skill["properties"])
    assert "paused" in tools["put_pmo_intake"].input_schema["properties"]["body"]["properties"]
    with pytest.raises(ValueError, match="unknown argument"):
        render_call(tools["health"], {"bogus": 1})


def test_operation_ids_are_unique_function_names(doc):
    ids = [o["operationId"] for o in _ops(doc).values()]
    assert len(ids) == len(set(ids))
    assert {"latest_cli", "latest_cli_get"} <= set(ids)          # the doubled function: bare + suffixed
    assert "health" in ids and "list_runs" in ids


def test_admin_client_sends_intent_and_actor_headers(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("ADMIN_USER=op\nADMIN_PASSWORD=pw\n")
    seen = {}

    class Resp(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class Opener:
        def open(self, req, timeout=None):
            seen["method"] = req.get_method(); seen["url"] = req.full_url
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            seen["body"] = req.data
            return Resp(b'{"ok": true}')
    monkeypatch.setattr(admin_api, "_opener", lambda: Opener())
    st, out = admin_api.request(tmp_path, "post", "/api/v1/x", query={"a": 1, "b": None},
                                body={"k": "v"}, actor="mcp")
    assert (st, out) == (200, {"ok": True})
    assert seen["method"] == "POST" and seen["url"].endswith("/api/v1/x?a=1")
    assert seen["headers"]["x-devcake-request"] == "1"
    assert seen["headers"]["x-devcake-actor"] == "mcp"
    assert seen["headers"]["authorization"].startswith("Basic ")
    assert json.loads(seen["body"]) == {"k": "v"}
    st, out = admin_api.request(tmp_path, "get", "/api/v1/y")
    assert "x-devcake-request" not in seen["headers"]


def test_mcp_verb_parses_and_fails_closed_without_the_sdk(monkeypatch, capsys, tmp_path):
    assert cli_main.main(["mcp", "--help"]) == 0
    assert "read-only" in capsys.readouterr().out
    assert cli_main.main(["mcp", "--bogus"]) == 2
    monkeypatch.chdir(tmp_path)                    # not a checkout
    assert cli_main.main(["mcp"]) == 3
    assert "not a DevCake checkout" in capsys.readouterr().err
    # a checkout without the optional extra: the install hint, exit 3
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "docker-bake.hcl").write_text("group \"default\" {}\n")
    import builtins
    real_import = builtins.__import__

    def no_mcp(name, *a, **k):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError(name)
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_mcp)
    assert cli_main.main(["mcp", "--read-only"]) == 3
    assert "devcake-cli[mcp]" in capsys.readouterr().err
