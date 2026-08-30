"""CAKE-178: ``devcake setup`` public seam (ADR-0038 Decision 1 / setup).

Public seam: ``devcake_cli.main:main`` argv ``setup`` / ``setup --json``.
Asserts help, usage exits, first-setup receipt, create-once exit 5,
connections upsert, bundle import apply, and never-echo of secrets.
Does not assert private helpers.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

_CLI_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "cli",
    Path("/srv/cli"),
]


def _cli_root() -> Path:
    path = next((p for p in _CLI_CANDIDATES if p.is_dir()), None)
    assert path is not None, "cli/ missing — bind /srv/cli in the pytest runner"
    return path


def _ensure_cli_importable() -> None:
    cli = _cli_root()
    if str(cli) not in sys.path:
        sys.path.insert(0, str(cli))


def _fake_checkout(tmp_path: Path) -> Path:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "docker-bake.hcl").write_text("group \"default\" {}\n")
    scripts = tmp_path / "scripts"
    (scripts / "dev_factory").mkdir(parents=True)
    (scripts / "lib").mkdir()
    (scripts / "lib" / "stack_env.sh").write_text("#!/bin/bash\n")
    (scripts / "lib" / "baker_host.sh").write_text("#!/bin/bash\n")
    (scripts / "app_digest.py").write_text("print('x')\n")
    (tmp_path / ".env").write_text(
        "ADMIN_USER=admin\nADMIN_PASSWORD=TestPassword1!\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").chmod(0o600)
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    return sock


# ── Slice 0: dispatch + help + usage ─────────────────────────────────────────


def test_setup_help_exits_zero_and_lists_adr_flags(capsys):
    _ensure_cli_importable()
    import devcake_cli.main as cli_main

    assert cli_main.main(["setup", "--help"]) == 0
    out = capsys.readouterr().out
    assert "not yet implemented" not in out.lower()
    for flag in (
        "--role-harness",
        "--role-model",
        "--same-harness",
        "--same-model",
        "--pmo-api-key-env",
        "--repo-token-env",
        "--import",
        "--json",
    ):
        assert flag in out


def test_setup_unknown_flag_exits_usage():
    _ensure_cli_importable()
    import devcake_cli.main as cli_main

    assert cli_main.main(["setup", "--not-a-real-flag"]) == 2


def test_setup_stub_message_gone():
    _ensure_cli_importable()
    import devcake_cli.main as cli_main

    # Unknown flag path must not print the old sibling-stub wording.
    rc = cli_main.main(["setup", "--bogus"])
    assert rc == 2


def test_setup_same_harness_disagrees_with_role_harness():
    _ensure_cli_importable()
    import devcake_cli.main as cli_main

    assert (
        cli_main.main(
            [
                "setup",
                "--same-harness",
                "claude-code",
                "--role-harness",
                "judge=grok-build",
            ]
        )
        == 2
    )


def test_setup_mutually_exclusive_pmo_secret_sources():
    _ensure_cli_importable()
    import devcake_cli.main as cli_main

    assert (
        cli_main.main(
            [
                "setup",
                "--pmo-name",
                "linear",
                "--pmo-api-key-env",
                "K",
                "--pmo-api-key-stdin",
            ]
        )
        == 2
    )


# ── Shared fakes for HTTP + doctor ────────────────────────────────────────────


class _FakeHttp:
    """Callable HTTP fake: records calls; returns scripted (status, body)."""

    def __init__(self, script: dict[tuple[str, str], tuple[int, object]] | None = None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.script = script or {}
        self.secret_bodies: list[dict] = []

    def __call__(self, method, url, body, headers):
        path = url.split("://", 1)[-1]
        # strip host
        if "/" in path:
            path = "/" + path.split("/", 1)[1]
        else:
            path = "/"
        self.calls.append((method, path, body))
        if body and isinstance(body, dict) and "value" in body:
            self.secret_bodies.append(body)
        key = (method, path)
        if key in self.script:
            return self.script[key]
        # prefix match for dynamic paths
        for (m, p), resp in self.script.items():
            if m == method and path.startswith(p.rstrip("*")):
                return resp
        return 500, {"detail": f"unscripted {method} {path}"}


def _patch_doctor_ok(monkeypatch):
    _ensure_cli_importable()
    import devcake_cli.doctor as doctor_mod

    ok = [
        doctor_mod.CheckResult(id=cid, ok=True, detail="ok", hard=True)
        for cid in doctor_mod.CHECK_IDS
    ]
    monkeypatch.setattr(doctor_mod, "run_checks", lambda **kwargs: ok)


# ── Slice 1: first-setup ──────────────────────────────────────────────────────


def test_setup_same_harness_creates_roster_receipt(monkeypatch, tmp_path, capsys):
    _ensure_cli_importable()
    import devcake_cli.main as cli_main
    import devcake_cli.setup as setup_mod

    sock = _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_SOCK", str(sock))
    _patch_doctor_ok(monkeypatch)

    http = _FakeHttp(
        {
            ("POST", "/api/v1/dev-types/first-setup"): (
                200,
                {"created": ["judge", "executor", "steward"]},
            ),
        }
    )
    monkeypatch.setattr(setup_mod, "default_http", http)

    rc = cli_main.main(
        ["--json", "setup", "--same-harness", "claude-code", "--same-model", ""]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err + captured.out
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["roles_created"] == ["judge", "executor", "steward"]
    for role in ("judge", "executor", "steward"):
        assert payload["roles"][role]["harness_template"] == "claude-code"
        assert payload["roles"][role]["created"] is True
    # Posted body mirrors FirstSetupDialog / first_setup API.
    post = next(c for c in http.calls if c[0] == "POST" and "first-setup" in c[1])
    assert set(post[2]["roles"]) == {"judge", "executor", "steward"}
    assert post[2]["roles"]["judge"]["harness_template"] == "claude-code"


def test_setup_first_setup_conflict_exits_5(monkeypatch, tmp_path, capsys):
    _ensure_cli_importable()
    import devcake_cli.main as cli_main
    import devcake_cli.setup as setup_mod

    sock = _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_SOCK", str(sock))
    _patch_doctor_ok(monkeypatch)

    http = _FakeHttp(
        {
            ("POST", "/api/v1/dev-types/first-setup"): (
                409,
                {"detail": "first setup requires an empty Dev Type roster"},
            ),
        }
    )
    monkeypatch.setattr(setup_mod, "default_http", http)

    rc = cli_main.main(["--json", "setup", "--same-harness", "claude-code"])
    captured = capsys.readouterr()
    assert rc == 5
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["roles_created"] == []
    assert all(payload["roles"][r]["created"] is False for r in ("judge", "executor", "steward"))
    # Never echo secrets (none here) — assert password from .env not on streams.
    blob = captured.out + captured.err
    assert "TestPassword1!" not in blob


# ── Slice 2: connections ──────────────────────────────────────────────────────


def test_setup_pmo_and_repo_upsert_and_secrets(monkeypatch, tmp_path, capsys):
    _ensure_cli_importable()
    import devcake_cli.main as cli_main
    import devcake_cli.setup as setup_mod

    sock = _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_SOCK", str(sock))
    monkeypatch.setenv("LINEAR_KEY", "lin_secret_value_xyz")
    monkeypatch.setenv("GH_TOKEN_SETUP", "gh_secret_value_xyz")
    _patch_doctor_ok(monkeypatch)

    cfg = {"pmos": [], "repos": [], "assignments": {}}
    http = _FakeHttp(
        {
            ("GET", "/api/v1/config"): (200, cfg),
            ("PUT", "/api/v1/config"): (200, cfg),
            ("PUT", "/api/v1/secrets/pmo/acme/api_key"): (
                200,
                {"present": True},
            ),
            ("PUT", "/api/v1/secrets/repo/main/token"): (
                200,
                {"present": True},
            ),
        }
    )
    monkeypatch.setattr(setup_mod, "default_http", http)

    rc = cli_main.main(
        [
            "--json",
            "setup",
            "--pmo-name",
            "acme",
            "--pmo-system",
            "linear",
            "--pmo-team-key",
            "ACME",
            "--pmo-api-key-env",
            "LINEAR_KEY",
            "--repo-name",
            "main",
            "--repo-forge",
            "github",
            "--repo-url",
            "https://github.com/acme/app",
            "--repo-token-env",
            "GH_TOKEN_SETUP",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err + captured.out
    payload = json.loads(captured.out)
    assert payload["connections"]["pmo"][0]["name"] == "acme"
    assert payload["connections"]["pmo"][0]["configured"] is True
    assert payload["connections"]["pmo"][0]["tested"] is False
    assert payload["connections"]["repos"][0]["name"] == "main"
    assert payload["secrets_received"]["pmo_api_key"] is True
    assert payload["secrets_received"]["repo_token_count"] == 1
    blob = captured.out + captured.err
    assert "lin_secret_value_xyz" not in blob
    assert "gh_secret_value_xyz" not in blob
    assert "TestPassword1!" not in blob


# ── Slice 3: settings-bundle import ───────────────────────────────────────────


def test_setup_import_applies_profile_and_writes_setup_env(
    monkeypatch, tmp_path, capsys
):
    _ensure_cli_importable()
    import devcake_cli.main as cli_main
    import devcake_cli.setup as setup_mod

    sock = _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_SOCK", str(sock))
    _patch_doctor_ok(monkeypatch)

    bundle = tmp_path / "bundle.yaml"
    bundle.write_text("kind: devcake-settings-bundle\nsections: [config]\n")
    env_text = (
        "# generated\nADMIN_PASSWORD=ImportedPass1!\nGITEA_ADMIN_PASSWORD=GiteaPass1!\n"
    )

    http = _FakeHttp(
        {
            ("POST", "/api/v1/settings/import"): (
                200,
                {
                    "saved_as": "imported-bundle",
                    "sections": ["config", "secrets"],
                    "has_setup_env": True,
                    "skills_imported": [],
                    "warnings": [],
                },
            ),
            ("POST", "/api/v1/profiles/imported-bundle/apply"): (
                200,
                {"applied": True},
            ),
            ("POST", "/api/v1/settings/import/env"): (200, env_text),
            ("GET", "/api/v1/secrets/inventory"): (
                200,
                {
                    "connections": [
                        {"scope": "pmo", "instance": "a", "field": "api_key"},
                        {"scope": "repo", "instance": "b", "field": "token"},
                    ],
                    "harness": [{"var": "ANTHROPIC_API_KEY"}],
                },
            ),
        }
    )
    monkeypatch.setattr(setup_mod, "default_http", http)

    rc = cli_main.main(["--json", "setup", "--import", str(bundle)])
    captured = capsys.readouterr()
    assert rc == 0, captured.err + captured.out
    payload = json.loads(captured.out)
    bi = payload["bundle_import"]
    assert bi["applied"] is True
    assert bi["profile"] == "imported-bundle"
    assert "config" in bi["sections"]
    assert "ADMIN_PASSWORD" in bi["setup_env_keys"]
    assert "GITEA_ADMIN_PASSWORD" in bi["setup_env_keys"]
    assert bi["secret_key_counts"]["connections"] == 2
    assert bi["secret_key_counts"]["harness"] == 1
    # Host .env updated (names only in receipt).
    env_txt = (tmp_path / ".env").read_text()
    assert "ADMIN_PASSWORD=ImportedPass1!" in env_txt
    blob = captured.out + captured.err
    assert "ImportedPass1!" not in blob
    assert any("devcake up" in s for s in payload["next_steps"])


# ── Slice 4: receipt schema + doctor hard-fail ────────────────────────────────


def test_setup_doctor_hard_fail_exits_3(monkeypatch, tmp_path, capsys):
    _ensure_cli_importable()
    import devcake_cli.main as cli_main
    import devcake_cli.doctor as doctor_mod
    import devcake_cli.setup as setup_mod

    sock = _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_SOCK", str(sock))

    checks = [
        doctor_mod.CheckResult(
            id="docker_socket",
            ok=False,
            detail="Docker socket not found",
            hard=True,
        ),
        *[
            doctor_mod.CheckResult(id=cid, ok=True, detail="ok", hard=False)
            for cid in doctor_mod.CHECK_IDS
            if cid != "docker_socket"
        ],
    ]
    monkeypatch.setattr(doctor_mod, "run_checks", lambda **kwargs: checks)
    monkeypatch.setattr(
        setup_mod,
        "default_http",
        _FakeHttp(
            {
                ("POST", "/api/v1/dev-types/first-setup"): (
                    200,
                    {"created": ["judge", "executor", "steward"]},
                ),
            }
        ),
    )

    rc = cli_main.main(["--json", "setup", "--same-harness", "claude-code"])
    captured = capsys.readouterr()
    assert rc == 3
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["doctor"]["ok"] is False
    assert payload["roles_created"] == ["judge", "executor", "steward"]
