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
    "apparmor_profile",
)


def test_doctor_help_exits_zero():
    _ensure_cli_importable()
    import devcake_cli.main as cli_main

    assert cli_main.main(["doctor", "--help"]) == 0


def _env_value_src() -> Path:
    """scripts/harness_probe/env_value.py — beside the checkout, or bound at
    /srv/repo-scripts in the container runner."""
    for p in (Path(__file__).resolve().parents[2] / "scripts" / "harness_probe" / "env_value.py",
              Path("/srv/repo-scripts/harness_probe/env_value.py")):
        if p.is_file():
            return p
    raise AssertionError("env_value.py not found — bind scripts → /srv/repo-scripts")


def _no_daemon(monkeypatch):
    """Hermetic: the AppArmor check never asks this developer's daemon."""
    from devcake_cli import doctor
    monkeypatch.setattr(doctor, "apparmor_facts", lambda **_: doctor.ApparmorFacts(
        enabled=False, loaded=None, installed=False, current=None, parser=False,
        compiles=None, applies=None))


def test_doctor_json_schema_and_catalog_ids(monkeypatch, tmp_path, capsys):
    """``doctor --json`` emits schema_version + checks with the sealed ids."""
    _ensure_cli_importable()
    import devcake_cli.main as cli_main
    _no_daemon(monkeypatch)

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
    _no_daemon(monkeypatch)

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
    assert "devcake up --release" in drift.detail

    (tmp_path / ".env").write_text("ADMIN_USER=a\nDEVCAKE_TAG=v0.5.9\n")
    same = doctor.check_version_pin(repo_root=tmp_path)
    assert same.ok and same.hard and "runs under it" in same.detail

    monkeypatch.setenv("DEVCAKE_TAG", "abc1234")               # a scratch build
    (tmp_path / ".env").write_text("DEVCAKE_TAG=abc1234\n")
    scratch = doctor.check_version_pin(repo_root=tmp_path)
    assert not scratch.ok and "override is set in this shell" in scratch.detail


def test_apparmor_profile_check_asks_the_daemon_first(tmp_path, monkeypatch):
    """docs/13: the Dev-container AppArmor profile is soft everywhere.
    Evidence order: the daemon (docker info says whether AppArmor is on at
    all; a throwaway container names the profile — definitive), the
    kernel's profile list (root-only on stock Ubuntu), then the installed
    file plus the host parser compiling the checkout's copy (an older
    parser rejects the userns rule: install succeeds, load fails). The
    check also flags a .env that names a profile the host cannot apply."""
    import subprocess
    from test_devcake_cli_setup import _ensure_cli_importable
    _ensure_cli_importable()
    from devcake_cli import doctor

    ours = tmp_path / "scripts" / "apparmor"
    ours.mkdir(parents=True)
    (ours / "devcake-nested").write_text("profile devcake-nested {}\n")
    enabled = tmp_path / "enabled"
    profiles = tmp_path / "profiles"
    installed = tmp_path / "etc-devcake-nested"
    world = {"info": '["name=apparmor","name=seccomp"]', "images": "devcake/dev-hello:v1\n",
             "run_rc": 0, "run_err": "", "parser_rc": 0}
    seen: list[list[str]] = []

    def run(cmd, **kw):
        seen.append(list(cmd))
        name = cmd[0].rsplit("/", 1)[-1]
        if name == "docker" and cmd[1] == "info":
            return subprocess.CompletedProcess(cmd, 0, world["info"], "")
        if name == "docker" and cmd[1] == "images":
            return subprocess.CompletedProcess(cmd, 0, world["images"], "")
        if name == "docker" and cmd[1] == "run":
            return subprocess.CompletedProcess(cmd, world["run_rc"], "", world["run_err"])
        if name == "apparmor_parser":
            return subprocess.CompletedProcess(cmd, world["parser_rc"], "", "")
        raise AssertionError(cmd)

    parser = "apparmor_parser"

    def facts():
        return doctor.apparmor_facts(repo_root=tmp_path, run=run, docker="docker",
                                     parser=parser, enabled_path=enabled,
                                     profiles_path=profiles, installed_path=installed)

    # the daemon has no AppArmor (WSL2, Desktop, SELinux distros) → ok, nothing asked
    world["info"] = '["name=seccomp"]'
    off = doctor.check_apparmor_profile(repo_root=tmp_path, facts=facts())
    assert off.ok and not off.hard and "no AppArmor on the Docker host" in off.detail
    assert not any(c[1] == "run" for c in seen)

    # the daemon is unreachable → the CLI host's kernel decides
    world["info"] = ""
    monkeypatch.setattr(doctor, "_daemon_has_apparmor", lambda *a: None)
    enabled.write_text("N\n")
    assert not facts().enabled
    monkeypatch.undo()

    world["info"] = '["name=apparmor","name=seccomp"]'
    missing = doctor.check_apparmor_profile(repo_root=tmp_path, facts=facts())
    assert not missing.ok and not missing.hard
    assert "unusable: it is not loaded" in missing.detail and "printed only" in missing.detail
    assert f"sudo install -m 0644 {ours / 'devcake-nested'} /etc/apparmor.d/" in missing.detail
    assert "sudo apparmor_parser -r /etc/apparmor.d/devcake-nested" in missing.detail

    # installed, kernel list unreadable, the daemon applies it → ok (definitive)
    installed.write_text("profile devcake-nested {}\n")
    ok = doctor.check_apparmor_profile(repo_root=tmp_path, facts=facts())
    assert ok.ok and "the daemon applies it" in ok.detail
    assert facts().usable and facts().applies is True and facts().compiles is True

    # installed but the daemon refuses it; the host parser compiles it →
    # the file was never loaded (or unloaded): the second command loads it,
    # and the daemon's own line is quoted
    world["run_rc"], world["run_err"] = 125, "unable to apply apparmor profile: no such file"
    refused = doctor.check_apparmor_profile(repo_root=tmp_path, facts=facts())
    assert not refused.ok and "daemon cannot apply it" in refused.detail
    assert "never loaded" in refused.detail and "4.0" not in refused.detail
    assert "[daemon: unable to apply apparmor profile: no such file]" in refused.detail
    assert not facts().usable
    # ANY failure of the throwaway counts as a refusal — the file tier is
    # never trusted once the daemon was asked
    world["run_err"] = "docker: Error response from daemon: cgroup limits unsupported"
    assert facts().applies is False and not facts().usable
    hiccup = doctor.check_apparmor_profile(repo_root=tmp_path, facts=facts())
    assert "did not attribute to AppArmor" in hiccup.detail and "cgroup limits" in hiccup.detail
    world["run_err"] = "unable to apply apparmor profile: no such file"
    # the host parser rejects it too → the parser is too old, say so
    world["parser_rc"] = 1
    old_parser = doctor.check_apparmor_profile(repo_root=tmp_path, facts=facts())
    assert "apparmor_parser rejects the profile (4.0 or newer" in old_parser.detail
    # ... and a .env that still names it is called out (the checkout's
    # dotenv reader is the one the baker and the probe use)
    (tmp_path / "scripts" / "harness_probe").mkdir(parents=True, exist_ok=True)
    import shutil as _sh
    _sh.copy(_env_value_src(), tmp_path / "scripts" / "harness_probe" / "env_value.py")
    (tmp_path / ".env").write_text("DEVCAKE_APPARMOR_PROFILE=devcake-nested\n")
    stale = doctor.check_apparmor_profile(repo_root=tmp_path, facts=facts())
    assert ".env still names it — run devcake up" in stale.detail
    (tmp_path / ".env").unlink()

    # no image to probe with and no parser on the host → the file stands in
    world["run_rc"], world["run_err"], world["parser_rc"] = 0, "", 0
    world["images"] = ""
    parser = None
    monkeypatch.setattr(doctor.shutil, "which", lambda *a, **k: "docker" if a[0] == "docker" else None)
    tier2 = doctor.check_apparmor_profile(repo_root=tmp_path, facts=facts())
    assert tier2.ok and "loaded state unreadable without root" in tier2.detail
    assert facts().compiles is None and not facts().parser
    monkeypatch.undo()
    parser = "apparmor_parser"
    world["images"] = "devcake/dev-hello:v1\n"

    installed.write_text("profile devcake-nested { # older }\n")
    outdated = doctor.check_apparmor_profile(repo_root=tmp_path, facts=facts())
    assert not outdated.ok and "outdated" in outdated.detail and "apparmor_parser -r" in outdated.detail
    installed.write_text("profile devcake-nested {}\n")

    # usable, but .env still names docker-default, or lacks the key → run devcake up
    import shutil as _sh
    _sh.copy(_env_value_src(), tmp_path / "scripts" / "harness_probe" / "env_value.py")
    (tmp_path / ".env").write_text("DEVCAKE_APPARMOR_PROFILE=docker-default\n")
    switch = doctor.check_apparmor_profile(repo_root=tmp_path, facts=facts())
    assert not switch.ok and "still names docker-default" in switch.detail
    (tmp_path / ".env").write_text("DEVCAKE_TAG=v1\n")
    missing_key = doctor.check_apparmor_profile(repo_root=tmp_path, facts=facts())
    assert not missing_key.ok and "does not name it yet" in missing_key.detail
    (tmp_path / ".env").write_text("DEVCAKE_APPARMOR_PROFILE=devcake-nested\n")
    assert doctor.check_apparmor_profile(repo_root=tmp_path, facts=facts()).ok
    (tmp_path / ".env").unlink()

    # the kernel list readable and naming it, daemon says yes
    profiles.write_text("docker-default (enforce)\ndevcake-nested (enforce)\n")
    loaded = doctor.check_apparmor_profile(repo_root=tmp_path, facts=facts())
    assert loaded.ok and facts().loaded is True
