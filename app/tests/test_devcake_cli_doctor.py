"""CAKE-177: ``devcake doctor`` named check catalog (ADR-0038 Decision 1/2).

Public seam: ``devcake_cli.main:main`` with argv ``doctor`` / ``doctor --json``.
Asserts stable check ids, remedy text on failure, JSON schema, exit 3 when a
hard check fails. Does not assert private helpers.
"""

from __future__ import annotations

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


_EXPECTED_CHECK_IDS = (
    "docker_socket",
    "docker_group",
    "docker_gid",
    "buildx",
    "checkout_layout",
    "digest_lockstep",
    "version_pin",
    "user_session_linger",
    "ports",
    "baker_liveness",
)


def test_doctor_help_exits_zero():
    _ensure_cli_importable()
    import devcake_cli.main as cli_main

    assert cli_main.main(["doctor", "--help"]) == 0


def test_doctor_json_schema_and_catalog_ids(monkeypatch, tmp_path, capsys):
    """``doctor --json`` emits schema_version + checks with the sealed ids."""
    _ensure_cli_importable()
    import devcake_cli.main as cli_main

    # Point checkout checks at a minimal fake repo root.
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "docker-bake.hcl").write_text("group \"default\" {}\n")
    scripts = tmp_path / "scripts"
    (scripts / "dev_factory").mkdir(parents=True)
    (scripts / "dev_factory" / "__init__.py").write_text("")
    (scripts / "app_digest.py").write_text("print('deadbeef')\n")
    (scripts / "lib").mkdir()
    (scripts / "lib" / "stack_env.sh").write_text("#!/bin/bash\n")

    monkeypatch.chdir(tmp_path)
    # Force socket missing so at least one hard check fails → exit 3.
    monkeypatch.setenv("DOCKER_SOCK", str(tmp_path / "no-such.sock"))

    rc = cli_main.main(["--json", "doctor"])
    captured = capsys.readouterr()
    assert rc == 3
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["ok"] is False
    ids = [c["id"] for c in payload["checks"]]
    assert ids == list(_EXPECTED_CHECK_IDS)
    for check in payload["checks"]:
        assert "ok" in check and "detail" in check
        assert isinstance(check["ok"], bool)
        assert isinstance(check["detail"], str)

    sock = next(c for c in payload["checks"] if c["id"] == "docker_socket")
    assert sock["ok"] is False
    assert "DOCKER_SOCK" in sock["detail"] or "docker" in sock["detail"].lower()


def test_doctor_failure_prints_remedy_human(monkeypatch, tmp_path, capsys):
    """Human mode prints a one-time remedy; never runs sudo/usermod/linger."""
    _ensure_cli_importable()
    import devcake_cli.main as cli_main

    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "docker-bake.hcl").write_text("group \"default\" {}\n")
    scripts = tmp_path / "scripts"
    (scripts / "dev_factory").mkdir(parents=True)
    (scripts / "app_digest.py").write_text("print('x')\n")
    (scripts / "lib").mkdir()
    (scripts / "lib" / "stack_env.sh").write_text("")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_SOCK", str(tmp_path / "missing.sock"))

    rc = cli_main.main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 3
    text = captured.out + captured.err
    assert "docker_socket" in text
    # Remedy style: tell the operator what to run / fix — CLI must not claim
    # it already ran usermod / enable-linger.
    assert "usermod" not in text.lower() or "do not run" in text.lower() or "printed" in text.lower() or "sudo" not in text
    assert "enable-linger" not in text or "loginctl enable-linger" in text


def test_version_pin_check_reports_drift_between_checkout_and_stack(tmp_path, monkeypatch):
    """docs/13: the checkout's VERSION is the pin; `.env`'s DEVCAKE_TAG is what
    the stack was brought up under. Agreement is ok; a drift is a soft fail
    with the one-line remedy; a missing pin or no stack are soft too."""
    from test_devcake_cli_setup import _ensure_cli_importable
    _ensure_cli_importable()
    from devcake_cli import doctor

    monkeypatch.delenv("DEVCAKE_TAG", raising=False)
    missing = doctor.check_version_pin(repo_root=tmp_path)
    assert missing.id == "version_pin" and not missing.ok and not missing.hard
    assert "VERSION missing" in missing.detail

    (tmp_path / "VERSION").write_text("v0.5.9\n")
    fresh = doctor.check_version_pin(repo_root=tmp_path)
    assert fresh.ok and not fresh.hard and "no stack brought up yet" in fresh.detail

    (tmp_path / ".env").write_text("ADMIN_USER=a\nDEVCAKE_TAG=v0.5.8\n")
    drift = doctor.check_version_pin(repo_root=tmp_path)
    assert not drift.ok and not drift.hard
    assert "pins v0.5.9" in drift.detail and "under v0.5.8" in drift.detail
    assert "devcake up --bake all" in drift.detail

    (tmp_path / ".env").write_text("ADMIN_USER=a\nDEVCAKE_TAG=v0.5.9\n")
    same = doctor.check_version_pin(repo_root=tmp_path)
    assert same.ok and same.hard and "runs under it" in same.detail

    monkeypatch.setenv("DEVCAKE_TAG", "abc1234")               # a scratch build
    (tmp_path / ".env").write_text("DEVCAKE_TAG=abc1234\n")
    scratch = doctor.check_version_pin(repo_root=tmp_path)
    assert not scratch.ok and "override is set in this shell" in scratch.detail
