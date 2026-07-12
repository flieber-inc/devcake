"""The harness registry is the single source of truth: image, credential
requirements, and OAuth flow derive from harness_template (docs/08 §2, §4)."""

import asyncio
from datetime import datetime, timezone
from typing import get_args

from devcake.config import AppConfig, DevType
from devcake.harness import HARNESSES, dev_type_status
from devcake.missions import MissionManager
from devcake.pmo import Mission
from devcake.state import RunStore


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def test_registry_covers_every_harness_literal():
    literals = get_args(DevType.model_fields["harness_template"].annotation)
    assert set(HARNESSES) == set(literals)


def test_oauth_flow_coherent_with_credential_files():
    for name, h in HARNESSES.items():
        if h.oauth is None:
            continue
        by_name = {cf.secret_file: cf for cf in h.credential_files}
        assert h.oauth.secret_file in by_name, name
        assert h.oauth.auth_path == by_name[h.oauth.secret_file].path_hint, name


def test_dev_type_status_derives_and_reports_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    secrets = tmp_path / "secrets" / "main-dev"
    secrets.mkdir(parents=True)
    (secrets / "grok-auth.json").write_text("{}")

    grok = dev_type_status(DevType(name="main-dev", harness_template="grok-build"))
    assert grok["harness"]["docker_image"] == "devcake/dev-grok-build:latest"
    assert grok["harness"]["oauth_available"] is True
    assert grok["secrets_present"] == ["grok-auth.json"]
    # slim dump: derived info lives only under "harness", never top-level
    assert "docker_image" not in grok
    assert "credential_env" not in grok

    claude = dev_type_status(DevType(name="senior-dev", harness_template="claude-code"))
    assert claude["harness"]["oauth_available"] is False
    assert claude["secrets_present"] == []


def test_legacy_yaml_keys_dropped_on_roundtrip():
    legacy = {"name": "senior-dev", "harness_template": "grok-build",
              "docker_image": "devcake/dev-claude-code:latest",   # stale — the bug
              "credential_env": ["CLAUDE_CODE_OAUTH_TOKEN"],
              "credential_files": [], "model": "grok-4.5"}
    dt = DevType.model_validate(legacy)
    dumped = dt.model_dump()
    assert dt.harness_template == "grok-build"
    for key in ("docker_image", "credential_env", "credential_files"):
        assert key not in dumped


def test_credential_spec_derives_from_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XAI_API_KEY", "xai-test-000000000000000000000")
    secrets = tmp_path / "secrets" / "main-dev"
    secrets.mkdir(parents=True)
    (secrets / "grok-auth.json").write_text('{"grok": true}')

    mgr = MissionManager.__new__(MissionManager)
    env, files = mgr._credential_spec(DevType(name="main-dev",
                                              harness_template="grok-build"))
    assert env == {"XAI_API_KEY": "xai-test-000000000000000000000"}
    assert files == [{"path_hint": "~/.grok/auth.json",
                      "content": '{"grok": true}', "mode": "600"}]

    # same dev type NAME, different harness → different requirements entirely
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    env, files = mgr._credential_spec(DevType(name="main-dev",
                                              harness_template="claude-code"))
    assert "CLAUDE_CODE_OAUTH_TOKEN" in env and "XAI_API_KEY" not in env
    assert files == []  # claude-code requires no credential files


def test_dispatch_mapper_uses_registry_image_and_sends_harness(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    captured = {}

    class FakeExecutor:
        async def start(self, params, dag_run_id):
            captured.update(params)

    class NullMessaging:
        async def create_run_user(self, rid):
            return "pw"

    class Runs:
        pass
    runs = Runs()
    runs.store = RunStore(tmp_path / "runs")
    runs.executor = FakeExecutor()

    mgr = MissionManager.__new__(MissionManager)
    mgr.config = AppConfig()
    mgr.runs = runs
    mgr.messaging = NullMessaging()

    dt = DevType(name="senior-dev", harness_template="grok-build",
                 model="grok-4.5")   # the user's exact scenario
    m = Mission(pmo_id="p1", pmo_kind="issue", key="T-1", title="t",
                status="backlog", labels={"DEVCAKE"},
                updated_at=datetime.now(timezone.utc))
    run = run_coro(mgr.dispatch_mapper(dt, [m]))

    assert captured["IMAGE"] == HARNESSES["grok-build"].image
    assert run.spec_env["DEVCAKE_HARNESS"] == "grok-build"
    assert run.spec_env["DEVCAKE_MODEL"] == "grok-4.5"
