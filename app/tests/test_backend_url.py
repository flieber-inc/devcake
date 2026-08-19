"""Dev Type backend URL (plan slice 4).

Empty = vendor default, no aim. Non-empty = full aim() (env, argv, files).
Dispatch ships the URL on the runspec; the Dev entrypoint calls aim() —
the app image does not import the Dev hexagon.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from devcake.config import AppConfig, DevType, PMOInstance, RepoInstance
from devcake.domain.run import Run
from fakes import FakeForgeRuntime, make_mission_manager


def test_backend_url_defaults_empty():
    dt = DevType(name="d", harness_template="grok-build")
    assert dt.backend_base_url == ""


def test_runspec_omits_empty_backend_url(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    payload = _runspec(tmp_path, backend_base_url="")
    assert "backend_base_url" not in payload


def test_runspec_carries_a_nonempty_backend_url(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    payload = _runspec(tmp_path, backend_base_url="http://vllm:8000/v1")
    assert payload["backend_base_url"] == "http://vllm:8000/v1"


def test_empty_url_aims_nothing():
    aim = _aim()
    got = aim.aim("grok-build", "", api_key="k", cli_version="0.2.112")
    assert got.env == {}
    assert got.extra_argv == ()
    assert got.files == ()


def test_nonempty_url_aims_grok_house_env_and_no_files():
    aim = _aim()
    got = aim.aim(
        "grok-build", "http://vllm:8000/v1", api_key="k",
        cli_version="0.2.112")
    assert got.env == {
        "GROK_MODELS_BASE_URL": "http://vllm:8000/v1",
        "XAI_API_KEY": "k",
    }
    assert got.files == ()


def test_url_without_api_key_is_refused():
    """Empty key must not write env/files or look like DEV_AUTH."""
    import pytest
    aim = _aim()
    with pytest.raises(ValueError, match="no API key"):
        aim.aim("grok-build", "http://vllm:8000/v1", api_key="",
                cli_version="0.2.112")


def test_toml_quotes_in_url_and_model_are_escaped():
    aim = _aim()
    got = aim.aim(
        "grok-build", 'http://x:8000/v1?q="y"', api_key="k",
        cli_version="1.0.4", model='stub"model')
    body = got.files[0].content
    assert 'base_url = "http://x:8000/v1?q=\\"y\\""' in body
    assert 'model = "stub\\"model"' in body


def test_grok_model_block_merges_into_existing_toml():
    aim = _aim()
    existing = (
        "[mcp.servers.logs]\n"
        'command = "logs-mcp"\n'
        "\n"
        "[model.old]\n"
        'model = "old"\n'
        'base_url = "http://old/v1"\n'
        'env_key = "XAI_API_KEY"\n'
    )
    got = aim.aim(
        "grok-build", "http://vllm:8000/v1", api_key="k",
        cli_version="1.0.4", model="stub-model")
    merged = aim.merge_grok_config_toml(existing, got.files[0].content)
    assert "[mcp.servers.logs]" in merged
    assert 'command = "logs-mcp"' in merged
    assert "[model.stub-model]" in merged
    assert "http://vllm:8000/v1" in merged
    assert "[model.old]" in merged


def test_opencode_bare_model_is_prefixed_for_aimed_provider():
    aim = _aim()
    assert aim.aimed_model_id("opencode", "llama-3") == "devcake/llama-3"
    assert aim.aimed_model_id("opencode", "devcake/llama-3") == "devcake/llama-3"
    assert aim.aimed_model_id("grok-build", "llama-3") == "llama-3"


def test_entrypoint_aim_writes_files_and_extra_argv(tmp_path, monkeypatch):
    """_apply_backend_aim is the prod seam: HOME files + aimed argv land."""
    ep = _entrypoint()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DEVCAKE_HARNESS", "pi")
    env = {
        "DEVCAKE_HARNESS": "pi",
        "DEVCAKE_MODEL": "stub-model",
        "OPENAI_API_KEY": "k-test",
    }
    extra = ep._apply_backend_aim(
        {"backend_base_url": "http://vllm:8000/v1"}, env, [], None)
    assert extra == ["--provider", "devcake"]
    assert env["OPENAI_API_KEY"] == "k-test"
    models = home / ".pi" / "agent" / "models.json"
    assert models.is_file()
    body = models.read_text()
    assert "http://vllm:8000/v1" in body
    assert "stub-model" in body


def test_entrypoint_aim_prefixes_opencode_model(tmp_path, monkeypatch):
    ep = _entrypoint()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    env = {
        "DEVCAKE_HARNESS": "opencode",
        "DEVCAKE_MODEL": "llama-3",
        "OPENAI_API_KEY": "k-test",
    }
    ep._apply_backend_aim(
        {"backend_base_url": "http://vllm:8000/v1"}, env, [], None)
    assert env["DEVCAKE_MODEL"] == "devcake/llama-3"
    cfg = home / ".config" / "opencode" / "opencode.json"
    assert cfg.is_file()
    assert "devcake" in cfg.read_text()


def test_entrypoint_aim_empty_key_exits_14_not_crash(tmp_path, monkeypatch):
    """URL without a key must not look like AUTH or exit 20."""
    import pytest
    ep = _entrypoint()
    monkeypatch.setenv("HOME", str(tmp_path))
    env = {
        "DEVCAKE_HARNESS": "grok-build",
        "DEVCAKE_MODEL": "stub",
        "DEVCAKE_CLI_VERSION": "0.2.112",
    }
    sent = []
    monkeypatch.setattr(ep, "send_artifacts", lambda p: sent.append(p))
    with pytest.raises(SystemExit) as ei:
        ep._apply_backend_aim(
            {"backend_base_url": "http://vllm:8000/v1"}, env, [], None)
    assert ei.value.code == 14
    assert sent and sent[0]["exit_code"] == 14
    assert "no API key" in sent[0]["error_detail"]
    assert sent[0]["error_class"] == "DEV_MCP_SETUP"


def test_entrypoint_aim_merges_grok_toml(tmp_path, monkeypatch):
    ep = _entrypoint()
    home = tmp_path / "home"
    grok = home / ".grok"
    grok.mkdir(parents=True)
    (grok / "config.toml").write_text(
        '[mcp.servers.logs]\ncommand = "logs-mcp"\n')
    monkeypatch.setenv("HOME", str(home))
    env = {
        "DEVCAKE_HARNESS": "grok-build",
        "DEVCAKE_MODEL": "stub-model",
        "DEVCAKE_CLI_VERSION": "1.0.4",
        "XAI_API_KEY": "k-test",
    }
    ep._apply_backend_aim(
        {"backend_base_url": "http://vllm:8000/v1"}, env, [], None)
    body = (grok / "config.toml").read_text()
    assert "[mcp.servers.logs]" in body
    assert "logs-mcp" in body
    assert "[model.stub-model]" in body
    assert "http://vllm:8000/v1" in body


def _entrypoint():
    import importlib.util
    roots = [
        Path(__file__).resolve().parents[2] / "images" / "common",
        Path("/srv/images/common"),
    ]
    root = next((p for p in roots if (p / "dev_entrypoint.py").is_file()), None)
    assert root is not None
    entry = root / "dev_entrypoint.py"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    keys = ("DEVCAKE_RUN_ID", "REDIS_URL", "REDIS_USER", "REDIS_PASSWORD")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.setdefault("DEVCAKE_RUN_ID", "T-AIM-EP")
    os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")
    os.environ.setdefault("REDIS_USER", "t")
    os.environ.setdefault("REDIS_PASSWORD", "t")
    spec = importlib.util.spec_from_file_location("dev_entrypoint_aim", entry)
    ep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ep)
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return ep


def _runspec(tmp_path, *, backend_base_url: str):
    from devcake import secrets as secrets_store
    secrets_store.write_connection_secret(
        "repo", "main", "token", "ghp_write_token_for_tests_0001")
    cfg = AppConfig()
    cfg.repos = [RepoInstance(name="main", url="https://github.com/o/r")]
    inst = PMOInstance(name="linear", team_key="DEV", repos=["main"])
    dts = {"d": DevType(
        name="d", harness_template="grok-build",
        backend_base_url=backend_base_url)}
    run = Run(run_id="T-1-1-EXECUTE-AAAAAA", mission_key="T-1",
              mission_type="EXECUTE", dev_type="d", seq=1)
    mgr = make_mission_manager(
        config=cfg, instance=inst, dev_types=dts,
        forge_runtime=FakeForgeRuntime(object(), inst=cfg.repos[0]))
    return mgr.runspec_secret_payload(run)


def _aim():
    os.environ.setdefault("DEVCAKE_RUN_ID", "T-AIM")
    roots = [
        Path(__file__).resolve().parents[2] / "images" / "common",
        Path("/srv/images/common"),
    ]
    root = next((p for p in roots if (p / "devcake_dev").is_dir()), None)
    assert root is not None
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from devcake_dev.harness import aim as aim_mod
    return aim_mod
