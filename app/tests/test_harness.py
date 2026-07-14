"""The harness registry is the single source of truth: image, credential
requirements, and OAuth flow derive from harness_template (docs/08 §2, §4)."""

import asyncio
from datetime import datetime, timezone
from typing import get_args

from devcake.config import AppConfig, DevType
from devcake.harness import HARNESSES, dev_type_status
from devcake.domain.orchestrator import MissionManager
from devcake.domain.model import Mission
from devcake.adapters.files.run_store import RunStore


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


def test_runspec_secret_payload_built_on_request(tmp_path, monkeypatch):
    """docs/09 §5: the secret half of a run spec is derived from current config
    whenever an authenticated active run asks — never stored, never expiring."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XAI_API_KEY", "xai-test-000000000000000000000")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_write_token_for_tests_0001")
    monkeypatch.setenv("GITHUB_TOKEN_RO", "ghp_readonly_token_for_tests_01")
    secrets = tmp_path / "secrets" / "main-dev"
    secrets.mkdir(parents=True)
    (secrets / "grok-auth.json").write_text('{"grok": true}')

    from devcake.domain.run import Run
    from devcake.config import RepoInstance
    mgr = MissionManager.__new__(MissionManager)
    cfg = AppConfig()
    cfg.repos[0] = RepoInstance(
        url="https://github.com/o/r", token_env="GITHUB_TOKEN",
        token_ro_env="GITHUB_TOKEN_RO")
    mgr.config = cfg
    mgr.dev_types = {"main-dev": DevType(name="main-dev",
                                         harness_template="grok-build"),
                     "senior-dev": DevType(name="senior-dev",
                                           harness_template="claude-code")}
    run = Run(run_id="T-1-1-EXECUTE-AAAAAA", mission_key="T-1",
              mission_type="EXECUTE", dev_type="main-dev", seq=1)
    payload = mgr.runspec_secret_payload(run)
    assert payload["env"]["XAI_API_KEY"] == "xai-test-000000000000000000000"
    assert payload["env"]["DEVCAKE_FORGE_TOKEN"] == "ghp_write_token_for_tests_0001"
    assert "OTEL_EXPORTER_OTLP_BASIC" in payload["env"]
    assert payload["credential_files"] == [{"path_hint": "~/.grok/auth.json",
                                            "content": '{"grok": true}',
                                            "mode": "600"}]
    # Non-EXECUTE stages: prefer RO token (clone-capable), never omit token
    for mtype in ("PLAN", "REVIEW", "ONBOARD", "MAPPER"):
        r2 = Run(run_id=f"T-1-1-{mtype}-AAAAAA", mission_key="T-1",
                 mission_type=mtype, dev_type="senior-dev", seq=1)
        p2 = mgr.runspec_secret_payload(r2)
        assert p2 is not None
        assert p2["env"]["DEVCAKE_FORGE_TOKEN"] == "ghp_readonly_token_for_tests_01"
    # token_ro_env unset but the conventional {token_env}_RO name is set:
    # the fallback must pick it up — the /health warning tells operators to
    # just set GITHUB_TOKEN_RO in .env, so that must actually work
    cfg.repos[0] = RepoInstance(
        url="https://github.com/o/r", token_env="GITHUB_TOKEN", token_ro_env=None)
    r3 = Run(run_id="T-1-1-PLAN-BBBBBB", mission_key="T-1",
             mission_type="PLAN", dev_type="senior-dev", seq=1)
    p3 = mgr.runspec_secret_payload(r3)
    assert p3["env"]["DEVCAKE_FORGE_TOKEN"] == "ghp_readonly_token_for_tests_01"
    # With no RO credential anywhere: fall back to the write token so private
    # repos still clone
    monkeypatch.delenv("GITHUB_TOKEN_RO")
    p4 = mgr.runspec_secret_payload(r3)
    assert p4["env"]["DEVCAKE_FORGE_TOKEN"] == "ghp_write_token_for_tests_0001"
    run.dev_type = "deleted-dev"
    assert mgr.runspec_secret_payload(run) is None


def test_runspec_get_served_while_active_and_refused_after(tmp_path):
    from devcake.domain.run import Run
    from devcake.domain.runs import RunManager

    replies = []

    class FakeMessaging:
        async def reply(self, run_id, kind, payload):
            replies.append((kind, payload))

        async def delete_runspec_result(self, rid):
            pass

    store = RunStore(tmp_path / "runs")
    manager = RunManager(store, FakeMessaging(), executor=None)
    run = Run(run_id="HELLO-1-1-HELLO-AAAAAA", mission_key="HELLO",
              mission_type="HELLO", dev_type="hello-stub", seq=1,
              spec_env={"PUBLIC": "yes"})
    run.state = "dispatched"
    store.save(run)

    run_coro(manager.handle(run.run_id, "runspec.get", {}))
    kind, payload = replies[-1]
    assert kind == "runspec.result"
    assert payload["env"]["PUBLIC"] == "yes"
    assert payload["env"]["FAKE_SECRET"] == f"devcake-fake-secret-{run.run_id}"
    assert payload["credential_files"][0]["path_hint"] == "~/.hello/creds.json"

    run.state = "finished"
    store.save(run)
    run_coro(manager.handle(run.run_id, "runspec.get", {}))
    assert replies[-1][0] == "runspec.error"


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
    # dispatch reads the forge descriptor for the dev-side dialect spec_env
    from devcake.adapters.github import GitHubForge
    mgr.forge = GitHubForge("https://github.com/o/r", "tok")

    dt = DevType(name="senior-dev", harness_template="grok-build",
                 model="grok-4.5")   # the user's exact scenario
    m = Mission(pmo_id="p1", pmo_kind="issue", key="T-1", title="t",
                status="backlog", labels={"DEVCAKE"},
                updated_at=datetime.now(timezone.utc))
    run = run_coro(mgr.dispatch_mapper(dt, [m]))

    assert captured["IMAGE"] == HARNESSES["grok-build"].image
    assert run.spec_env["DEVCAKE_HARNESS"] == "grok-build"
    assert run.spec_env["DEVCAKE_MODEL"] == "grok-4.5"
    # dev-side forge dialect flows via spec_env from the descriptor (docs/06/07)
    assert run.spec_env["DEVCAKE_CLONE_USER"] == "x-access-token"
    assert run.spec_env["DEVCAKE_GIT_EMAIL"] == "devcake@users.noreply.github.com"
    assert run.spec_env["DEVCAKE_FORGE_CLI_ENVS"] == "GH_TOKEN"
    assert run.spec_env["DEVCAKE_DEFAULT_BRANCH"] == "main"
