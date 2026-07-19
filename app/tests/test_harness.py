"""The harness registry is the single source of truth: image, credential
requirements, and OAuth flow derive from harness_template (docs/08 §2, §4)."""

import asyncio
from datetime import datetime, timezone
from typing import get_args

from devcake.config import PMOInstance, AppConfig, DevType
from fakes import FakeForgeRuntime
from devcake.harness import HARNESSES, dev_type_status
from devcake.domain.orchestrator import MissionManager
from devcake.domain.model import Mission
from devcake.adapters.files.run_store import RunStore
from devcake.domain.orchestrator import dispatch


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def test_registry_covers_every_harness_literal():
    literals = get_args(DevType.model_fields["harness_template"].annotation)
    assert set(HARNESSES) == set(literals)


def test_dev_type_status_credentials_ready(monkeypatch, tmp_path):
    """Overview 'Devs' card (v0.1.1 B3): readiness is server-computed —
    any ONE of the harness's env keys in the store, or any credential file
    present, is enough (the DevTypeCard rule, moved out of the SPA)."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as s
    dt = DevType(name="senior-dev", harness_template="claude-code")
    assert dev_type_status(dt)["credentials_ready"] is False
    s.write_harness_secret("ANTHROPIC_API_KEY", "sk-ant-value-1234")
    assert dev_type_status(dt)["credentials_ready"] is True

    g = DevType(name="grokdev", harness_template="grok-build")
    assert dev_type_status(g)["credentials_ready"] is False
    d = tmp_path / "secrets" / "grokdev"
    d.mkdir(parents=True)
    (d / "grok-auth.json").write_text("{}")
    assert dev_type_status(g)["credentials_ready"] is True


def test_harness_images_honor_devcake_tag(monkeypatch):
    """Audit A7: the documented pin workflow (export DEVCAKE_TAG=<sha> +
    bake + compose up) tags every image, but dispatch hardcoded :latest —
    a pinned install silently ran stale harnesses, or (no local :latest)
    pulled from the unclaimed public devcake/* Docker Hub namespace."""
    import importlib
    import devcake.harness as harness_mod
    monkeypatch.setenv("DEVCAKE_TAG", "abc1234")
    try:
        importlib.reload(harness_mod)
        assert all(h.image.endswith(":abc1234")
                   for h in harness_mod.HARNESSES.values())
        assert harness_mod.HARNESSES["claude-code"].image == \
            "devcake/dev-claude-code:abc1234"
    finally:
        monkeypatch.delenv("DEVCAKE_TAG")
        importlib.reload(harness_mod)


def test_hello_image_honors_devcake_tag(monkeypatch):
    import importlib
    import devcake.domain.runs as runs_mod
    monkeypatch.setenv("DEVCAKE_TAG", "abc1234")
    monkeypatch.delenv("DEVCAKE_HELLO_IMAGE", raising=False)
    try:
        importlib.reload(runs_mod)
        assert runs_mod.HELLO_IMAGE == "devcake/dev-hello:abc1234"
    finally:
        monkeypatch.delenv("DEVCAKE_TAG")
        importlib.reload(runs_mod)


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
    # the SPA gates the Skills selector on this (falsy = disabled)
    assert grok["harness"]["skills_dir"] == ".agents/skills"
    assert claude["harness"]["skills_dir"] == ".claude/skills"


def test_registry_skills_dirs():
    """The verified per-CLI read-set (this pin set): claude-code 2.1.210
    reads only ~/.claude/skills; grok 0.2.103 and codex 0.144.4 read
    ~/.agents/skills. Every declared dir must be a home-relative POSIX
    subpath — the entrypoint refuses anything else and falls back."""
    from pathlib import PurePosixPath
    assert HARNESSES["claude-code"].skills_dir == ".claude/skills"
    assert HARNESSES["grok-build"].skills_dir == ".agents/skills"
    assert HARNESSES["codex"].skills_dir == ".agents/skills"
    for name, h in HARNESSES.items():
        if h.skills_dir is None:
            continue
        p = PurePosixPath(h.skills_dir)
        assert p.parts and not p.is_absolute() and ".." not in p.parts, name


def test_dev_type_status_reports_secret_env_presence(tmp_path, monkeypatch):
    """The Config page renders ✓/✗ per declared secret env var — presence is
    server-computed (never the value) and kept OUT of credentials_ready:
    secret_env vars are mission tooling (e.g. a Datadog key), not harness
    credentials, so they must not flip Dev readiness either way."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as s
    dt = DevType(name="senior-dev", harness_template="claude-code",
                 secret_env=["DD_API_KEY", "DD_APP_KEY"])
    st = dev_type_status(dt)
    assert st["secret_env_present"] == {"DD_API_KEY": False,
                                        "DD_APP_KEY": False}
    s.write_harness_secret("DD_API_KEY", "dd-api-key-0123456789abcdef")
    st2 = dev_type_status(dt)
    assert st2["secret_env_present"] == {"DD_API_KEY": True,
                                         "DD_APP_KEY": False}
    assert st2["credentials_ready"] is False   # tooling never implies ready


def test_credential_spec_derives_from_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    secrets_store.write_harness_secret("XAI_API_KEY", "xai-test-000000000000000000000")
    secrets = tmp_path / "secrets" / "main-dev"
    secrets.mkdir(parents=True)
    (secrets / "grok-auth.json").write_text('{"grok": true}')

    from fakes import make_mission_manager
    mgr = make_mission_manager(noop_audit=False)
    env, files = dispatch._credential_spec(mgr, DevType(name="main-dev",
                                              harness_template="grok-build"))
    assert env == {"XAI_API_KEY": "xai-test-000000000000000000000"}
    assert files == [{"path_hint": "~/.grok/auth.json",
                      "content": '{"grok": true}', "mode": "600"}]

    # same dev type NAME, different harness → different requirements entirely
    secrets_store.write_harness_secret("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    env, files = dispatch._credential_spec(mgr, DevType(name="main-dev",
                                              harness_template="claude-code"))
    assert "CLAUDE_CODE_OAUTH_TOKEN" in env and "XAI_API_KEY" not in env
    assert files == []  # claude-code requires no credential files


def test_credential_spec_includes_dev_type_secret_env(tmp_path, monkeypatch):
    """DevType.secret_env — named refs into the GUI harness store, delivered
    alongside the harness credentials so mcp_setup_commands can reference
    e.g. $DD_API_KEY (ADR-0011: the value never touches config.yaml).
    Missing value = warn-and-proceed: a mission must not hard-fail over a
    log credential; the Config page shows the gap (secret_env_present)."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    secrets_store.write_harness_secret("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    secrets_store.write_harness_secret("DD_API_KEY", "dd-api-key-0123456789abcdef")

    from fakes import make_mission_manager
    mgr = make_mission_manager(noop_audit=False)
    dt = DevType(name="senior-dev", harness_template="claude-code",
                 secret_env=["DD_API_KEY", "DD_MISSING"])
    env, files = dispatch._credential_spec(mgr, dt)
    assert env["DD_API_KEY"] == "dd-api-key-0123456789abcdef"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"   # harness creds untouched
    assert "DD_MISSING" not in env                   # warn, never crash
    assert files == []


def test_missing_referenced_secret_env_rule(tmp_path, monkeypatch):
    """The v1 reference rule: $VAR and ${VAR} count, word-bounded — so
    $DD_KEY_STAGING is NOT a reference to DD_KEY; a stored value or an
    unreferenced name never gates."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as s
    from devcake.harness import missing_referenced_secret_env
    dt = DevType(name="x", harness_template="claude-code",
                 secret_env=["DD_API_KEY", "DD_APP_KEY", "UNUSED"],
                 mcp_setup_commands=[
                     "pip install --user devcake-logs-mcp",
                     "claude mcp add logs -e A=$DD_API_KEY "
                     "-e B=${DD_APP_KEY} -- python -m some_plugin.server"])
    assert missing_referenced_secret_env(dt) == ["DD_API_KEY", "DD_APP_KEY"]
    s.write_harness_secret("DD_API_KEY", "k1")
    assert missing_referenced_secret_env(dt) == ["DD_APP_KEY"]
    s.write_harness_secret("DD_APP_KEY", "k2")
    assert missing_referenced_secret_env(dt) == []
    dt2 = DevType(name="x", harness_template="claude-code",
                  secret_env=["DD_KEY"],
                  mcp_setup_commands=["echo $DD_KEY_STAGING"])
    assert missing_referenced_secret_env(dt2) == []


def test_runspec_secret_payload_built_on_request(tmp_path, monkeypatch):
    """docs/09 §5: the secret half of a run spec is derived from current config
    whenever an authenticated active run asks — never stored, never expiring."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    secrets_store.write_harness_secret("XAI_API_KEY", "xai-test-000000000000000000000")
    secrets_store.write_connection_secret("repo", "main", "token", "ghp_write_token_for_tests_0001")
    secrets_store.write_connection_secret("repo", "main", "token_ro", "ghp_readonly_token_for_tests_01")
    secrets = tmp_path / "secrets" / "main-dev"
    secrets.mkdir(parents=True)
    (secrets / "grok-auth.json").write_text('{"grok": true}')

    from fakes import make_mission_manager
    from devcake.domain.run import Run
    from devcake.config import RepoInstance
    cfg = AppConfig()
    cfg.repos = [RepoInstance(name="main", url="https://github.com/o/r")]
    mgr = make_mission_manager(
        config=cfg,
        instance=PMOInstance(name="linear", team_key="DEV", repos=["main"]),
        forge_runtime=FakeForgeRuntime(object(), inst=cfg.repos[0]),
        dev_types={
            "main-dev": DevType(name="main-dev", harness_template="grok-build"),
            "senior-dev": DevType(name="senior-dev", harness_template="claude-code"),
        },
    )
    run = Run(run_id="T-1-1-EXECUTE-AAAAAA", mission_key="T-1",
              mission_type="EXECUTE", dev_type="main-dev", seq=1)
    payload = mgr.runspec_secret_payload(run)
    assert payload["env"]["XAI_API_KEY"] == "xai-test-000000000000000000000"
    assert payload["env"]["DEVCAKE_FORGE_TOKEN"] == "ghp_write_token_for_tests_0001"
    # ISSUES #13 regression tripwire: OO credentials must NEVER re-enter a
    # runspec — Devs export through the collector, credential-free
    assert "OTEL_EXPORTER_OTLP_BASIC" not in payload["env"]
    assert not any("OO_" in k for k in payload["env"])
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
    # With no RO token stored: non-EXECUTE stages fall back to the WRITE
    # token so private repos still clone (the /health warning covers the
    # posture)
    secrets_store.write_connection_secret("repo", "main", "token_ro", "")
    mgr.forges = FakeForgeRuntime(object(), inst=cfg.repos[0])
    r3 = Run(run_id="T-1-1-PLAN-BBBBBB", mission_key="T-1",
             mission_type="PLAN", dev_type="senior-dev", seq=1)
    p4 = mgr.runspec_secret_payload(r3)
    assert p4["env"]["DEVCAKE_FORGE_TOKEN"] == "ghp_write_token_for_tests_0001"
    run.dev_type = "deleted-dev"
    assert mgr.runspec_secret_payload(run) is None


def test_runspec_payload_carries_mcp_setup_commands(tmp_path, monkeypatch):
    """The Dev entrypoint has consumed spec["mcp_setup_commands"] since M3
    (exit 14 on failure) — this is the missing producer half: the Dev Type's
    commands ride the runspec on BOTH forge branches (external and internal
    fallback), live-read like secrets so an operator's fix applies to the
    next runspec.get without redispatch."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    secrets_store.write_connection_secret(
        "repo", "main", "token", "ghp_write_token_for_tests_0001")
    secrets_store.write_harness_secret(
        "DD_API_KEY", "dd-api-key-0123456789abcdef")

    from types import SimpleNamespace
    from fakes import make_mission_manager
    from devcake.domain.run import Run
    from devcake.config import RepoInstance
    cmds = ["claude mcp add devcake-logs -e DD_API_KEY=$DD_API_KEY "
            "-- devcake-logs-mcp"]
    cfg = AppConfig()
    cfg.repos = [RepoInstance(name="main", url="https://github.com/o/r")]
    inst = PMOInstance(name="linear", team_key="DEV", repos=["main"])
    dts = {"senior-dev": DevType(name="senior-dev",
                                 harness_template="claude-code",
                                 mcp_setup_commands=cmds,
                                 secret_env=["DD_API_KEY"])}
    run = Run(run_id="T-1-1-EXECUTE-AAAAAA", mission_key="T-1",
              mission_type="EXECUTE", dev_type="senior-dev", seq=1)

    mgr = make_mission_manager(
        config=cfg, instance=inst, dev_types=dts,
        forge_runtime=FakeForgeRuntime(object(), inst=cfg.repos[0]))
    payload = mgr.runspec_secret_payload(run)
    assert payload["mcp_setup_commands"] == cmds
    assert payload["env"]["DD_API_KEY"] == "dd-api-key-0123456789abcdef"

    # internal-fallback branch delivers them too
    fr = FakeForgeRuntime(object(), inst=cfg.repos[0])
    fr.internal = {"main"}

    class FakeInternalForge:
        def mission_credentials(self, repo_ref):
            return SimpleNamespace(token_write="w", token_read="r")

        def activity_credentials(self, repo_name):
            return None                       # boot mint absent

    mgr2 = make_mission_manager(
        config=cfg, instance=inst, dev_types=dts,
        forge_runtime=fr, internal_forge=FakeInternalForge())
    assert mgr2.runspec_secret_payload(run)["mcp_setup_commands"] == cmds

    # no commands configured → key absent (deploy-skew-safe: the entrypoint
    # spec.get()s it with a default)
    plain = {"senior-dev": DevType(name="senior-dev",
                                   harness_template="claude-code")}
    mgr3 = make_mission_manager(
        config=cfg, instance=inst, dev_types=plain,
        forge_runtime=FakeForgeRuntime(object(), inst=cfg.repos[0]))
    assert "mcp_setup_commands" not in mgr3.runspec_secret_payload(run)


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
    assert payload["mcp_setup_commands"] == []   # HELLO stub: no-op loop

    run.state = "finished"
    store.save(run)
    run_coro(manager.handle(run.run_id, "runspec.get", {}))
    assert replies[-1][0] == "runspec.error"


def test_runspec_result_delivers_mcp_setup_commands(tmp_path):
    """Protocol seam: the finalizer payload's mcp_setup_commands reach the
    Dev verbatim as a top-level runspec.result key (the entrypoint consumes
    spec["mcp_setup_commands"]); payloads without the key yield []."""
    from devcake.domain.run import Run
    from devcake.domain.runs import RunManager

    replies = []

    class FakeMessaging:
        async def reply(self, run_id, kind, payload):
            replies.append((kind, payload))

        async def delete_runspec_result(self, rid):
            pass

    class FakeFinalizer:
        def runspec_secret_payload(self, run):
            return {"env": {"DD_API_KEY": "v"}, "credential_files": [],
                    "mcp_setup_commands": ["claude mcp add x -- y"]}

    store = RunStore(tmp_path / "runs")
    manager = RunManager(store, FakeMessaging(), executor=None,
                         finalizer=FakeFinalizer())
    run = Run(run_id="T-1-1-EXECUTE-AAAAAA", mission_key="T-1",
              mission_type="EXECUTE", dev_type="senior-dev", seq=1,
              spec_env={"PUBLIC": "yes"})
    run.state = "dispatched"
    store.save(run)

    run_coro(manager.handle(run.run_id, "runspec.get", {}))
    kind, payload = replies[-1]
    assert kind == "runspec.result"
    assert payload["mcp_setup_commands"] == ["claude mcp add x -- y"]
    assert payload["env"] == {"PUBLIC": "yes", "DD_API_KEY": "v"}


def test_dispatch_mapper_uses_registry_image_and_sends_harness(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    captured = {}

    class FakeExecutor:
        async def start(self, params, dag_run_id):
            captured.update(params)

    class NullMessaging:
        async def create_run_user(self, rid):
            return "pw"

    from devcake.domain.runs import RunManager

    store = RunStore(tmp_path / "runs")
    runs = RunManager(store, NullMessaging(), FakeExecutor())

    from fakes import make_mission_manager
    from devcake.adapters.github import GitHubForge
    mgr = make_mission_manager(
        runs=runs, messaging=NullMessaging(),
        forge=GitHubForge("https://github.com/o/r", "tok"),
        config=AppConfig(),
    )

    dt = DevType(name="senior-dev", harness_template="grok-build",
                 model="grok-4.5")   # the user's exact scenario
    m = Mission(pmo_id="p1", pmo_kind="issue", key="T-1", title="t",
                status="backlog", labels={"DEVCAKE"},
                updated_at=datetime.now(timezone.utc))
    run = run_coro(mgr.dispatch_mapper(dt, [m]))

    assert captured["IMAGE"] == HARNESSES["grok-build"].image
    # skills dir snapshotted from the same registry read as the image
    assert run.spec_skills_dir == HARNESSES["grok-build"].skills_dir
    assert run.spec_env["DEVCAKE_HARNESS"] == "grok-build"
    assert run.spec_env["DEVCAKE_MODEL"] == "grok-4.5"
    # dev-side forge dialect flows via spec_env from the descriptor (docs/06/07)
    assert run.spec_env["DEVCAKE_CLONE_USER"] == "x-access-token"
    assert run.spec_env["DEVCAKE_GIT_EMAIL"] == "devcake@users.noreply.github.com"
    assert run.spec_env["DEVCAKE_FORGE_CLI_ENVS"] == "GH_TOKEN"
    assert run.spec_env["DEVCAKE_DEFAULT_BRANCH"] == "main"
    assert run.auth_digest is not None
    assert store.get(run.run_id) is not None


def test_protocol_spec_env_points_devs_at_collector(monkeypatch):
    """ISSUES #13: Dev OTLP export targets the collector on devcake_runtime —
    never OpenObserve directly (which would need credentials in the runspec)."""
    from fakes import make_mission_manager
    from devcake.adapters.registry import make_forge
    from devcake.config import RepoInstance
    mgr = make_mission_manager(config=AppConfig(), noop_audit=False)
    repo = RepoInstance(url="https://github.com/o/r")
    env = dispatch._protocol_spec_env(mgr, 
        mission_id="p1", mission_key="T-1", mission_type="EXECUTE",
        dev_type=DevType(name="main-dev", harness_template="grok-build"),
        seq=1, extra_args="", repo=repo, forge=make_forge(repo))
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://otel-collector:4318/v1/traces"
    assert not any("openobserve" in v for v in env.values())


def test_harness_default_model_flows_into_spec_env(tmp_path):
    """UX item 2 (2026-07-15): grok-build runs grok-4.5 unless the Dev Type
    pins its own model; an explicit Dev Type model still wins."""
    from test_transitions import make_mgr, mission
    assert HARNESSES["grok-build"].default_model == "grok-4.5"
    mgr, _f, _s = make_mgr(tmp_path, mission())
    from devcake.config import RepoInstance
    repo = RepoInstance(name="main", url="https://github.com/o/r")
    forge = type("F", (), {"descriptor": type("D", (), {
        "clone_user": "x", "git_user_name": "n", "git_email": "e",
        "cli_token_envs": ["T"]})()})()
    grok = DevType(name="g", harness_template="grok-build")
    env = dispatch._protocol_spec_env(mgr, mission_id="p", mission_key="T-1",
                                 mission_type="EXECUTE", dev_type=grok, seq=1,
                                 extra_args="", repo=repo, forge=forge)
    assert env["DEVCAKE_MODEL"] == "grok-4.5"
    pinned = DevType(name="g2", harness_template="grok-build", model="grok-5")
    env2 = dispatch._protocol_spec_env(mgr, mission_id="p", mission_key="T-1",
                                  mission_type="EXECUTE", dev_type=pinned, seq=1,
                                  extra_args="", repo=repo, forge=forge)
    assert env2["DEVCAKE_MODEL"] == "grok-5"
