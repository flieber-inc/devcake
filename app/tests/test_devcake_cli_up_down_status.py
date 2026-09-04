"""CAKE-177: ``devcake up/down/status`` public seams (up.sh removed — cutover pin)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_CLI_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "cli",
    Path("/srv/cli"),
]
_UP_SH_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "up.sh",
    Path("/srv/up.sh"),
]


def _ensure_cli_importable() -> None:
    cli = next(p for p in _CLI_CANDIDATES if p.is_dir())
    if str(cli) not in sys.path:
        sys.path.insert(0, str(cli))


def _fake_checkout(tmp_path: Path) -> Path:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "docker-bake.hcl").write_text("group \"default\" {}\n")
    scripts = tmp_path / "scripts"
    (scripts / "dev_factory").mkdir(parents=True)
    (scripts / "lib").mkdir()
    (scripts / "lib" / "stack_env.sh").write_text(
        "#!/bin/bash\n"
        "devcake_docker_gid() { echo 4242; }\n"
        "devcake_docker_gid_incontainer() { echo 4242; }\n"
        "devcake_ws_host() { echo \"$2/workspaces\"; }\n"
    )
    (scripts / "lib" / "baker_host.sh").write_text("#!/bin/bash\n")
    (scripts / "lib" / "oo_password.sh").write_text("#!/bin/bash\n")
    (scripts / "app_digest.py").write_text("print('deadbeef')\n")
    (tmp_path / ".env.example").write_text(
        "ADMIN_USER=admin\nADMIN_PASSWORD=\nREDIS_PASSWORD=\n"
        "DAGU_PASSWORD=\nOO_ROOT_PASSWORD=\nOO_INGEST_EMAIL=\n"
        "OO_INGEST_PASSWORD=\nGITEA_ADMIN_PASSWORD=\n"
    )
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    return sock


def test_up_help_and_bad_flag():
    _ensure_cli_importable()
    import devcake_cli.main as cli_main

    assert cli_main.main(["up", "--help"]) == 0
    assert cli_main.main(["up", "--not-a-real-flag"]) == 2


def test_up_dry_run_no_mutation(monkeypatch, tmp_path, capsys):
    _ensure_cli_importable()
    import devcake_cli.main as cli_main
    import devcake_cli.up as up_mod

    sock = _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_SOCK", str(sock))

    # Point discovery at the fake stack_env helpers via real bash source.
    rc = cli_main.main(["--json", "up", "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err + captured.out
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["dry_run"] is True
    assert payload["docker_gid"] == "4242"
    assert payload["bake"] is False
    # dry-run must not create .env
    assert not (tmp_path / ".env").exists()


def test_up_dry_run_bake_plan(monkeypatch, tmp_path, capsys):
    _ensure_cli_importable()
    import devcake_cli.main as cli_main

    sock = _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_SOCK", str(sock))

    rc = cli_main.main(["up", "--dry-run", "--bake", "app", "admin"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err + captured.out
    text = captured.out + captured.err
    assert "docker buildx bake app admin" in text
    assert "would upsert DOCKER_GID=4242" in text


def test_down_invokes_compose_without_volume_wipe(monkeypatch, tmp_path, capsys):
    _ensure_cli_importable()
    import devcake_cli.main as cli_main
    import devcake_cli.down as down_mod

    _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(down_mod.subprocess, "run", _fake_run)
    rc = cli_main.main(["--json", "down"])
    captured = capsys.readouterr()
    assert rc == 0
    assert calls == [["docker", "compose", "down"]]
    assert "-v" not in calls[0]
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["volumes_removed"] is False


def test_status_json_fields(monkeypatch, tmp_path, capsys):
    _ensure_cli_importable()
    import devcake_cli.main as cli_main
    import devcake_cli.status as status_mod

    _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)

    def _fake_run(argv, **kwargs):
        if argv[:3] == ["docker", "compose", "ps"]:
            return subprocess.CompletedProcess(argv, 0, stdout='{"Name":"app"}\n')
        return subprocess.CompletedProcess(argv, 1, stderr="no")

    monkeypatch.setattr(status_mod.subprocess, "run", _fake_run)
    rc = cli_main.main(["status", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert "baker_alive" in payload
    assert payload["compose_ok"] is True


def test_up_sh_is_gone():
    """ADR-0038 Decision 4 cutover: the shim is removed — `devcake up` is
    the only bring-up entry. A resurrected up.sh would be a second body."""
    path = next((p for p in _UP_SH_CANDIDATES if p.is_file()), None)
    assert path is None, f"up.sh must not exist (found {path})"


def test_status_reports_the_pmo_request_budgets(monkeypatch, tmp_path, capsys):
    """ADR-0040 visibility: `devcake status` reads /health through the
    loopback admin proxy and prints one line per credential bucket plus the
    alarm text; --json carries the rows verbatim."""
    _ensure_cli_importable()
    import devcake_cli.main as cli_main
    import devcake_cli.status as status_mod

    _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)

    def _fake_run(argv, **kwargs):
        if argv[:3] == ["docker", "compose", "ps"]:
            return subprocess.CompletedProcess(argv, 0, stdout='{"Name":"app"}\n')
        return subprocess.CompletedProcess(argv, 1, stderr="no")

    health = {
        "pmo_budget": {
            "tracker.example/user:u1": {
                "label": "tracker.example/user:u1", "instances": ["a", "b"],
                "limit": 2500, "remaining": 2471, "blocked_until": None,
                "limited_last_hour": 2,
                "demand_per_hour": {"a": 61, "b": 1541}},
            "forge.example/key-0702": {
                "label": "forge.example/key-0702", "instances": ["board"],
                "limit": None, "remaining": None, "blocked_until": None,
                "limited_last_hour": 0, "demand_per_hour": {"board": None}}},
        "pmo_rate_limited": {
            "tracker.example/user:u1": "the tracker rejected 2 requests in the last hour"},
    }
    monkeypatch.setattr(status_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(status_mod, "_fetch_health", lambda root, **kw: health)
    rc = cli_main.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert ("  tracker.example/user:u1: about 1602 requests/hour (a 61, b 1541) "
            "against 2500/hour, 2471 remaining; rejected by the tracker in the "
            "last hour: 2") in out
    assert "    ! the tracker rejected 2 requests in the last hour" in out
    assert ("  forge.example/key-0702: about 0 requests/hour (board measuring) "
            "against no published limit; rejected by the tracker in the last "
            "hour: 0") in out
    rc = cli_main.main(["status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["health_reachable"] is True
    assert payload["pmo_budget"] == health["pmo_budget"]
    assert payload["pmo_rate_limited"] == health["pmo_rate_limited"]


def test_status_says_when_the_budget_is_unavailable(monkeypatch, tmp_path, capsys):
    _ensure_cli_importable()
    import devcake_cli.main as cli_main
    import devcake_cli.status as status_mod

    _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(status_mod.subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 0, stdout="{}\n"))
    monkeypatch.setattr(status_mod, "_fetch_health", lambda root, **kw: None)
    assert cli_main.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "pmo_budget: unavailable" in out and "127.0.0.1:8080" in out
    cli_main.main(["status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["health_reachable"] is False and payload["pmo_budget"] is None


def test_fetch_health_never_raises(monkeypatch, tmp_path):
    """A stack that is down is a status line, never a traceback."""
    _ensure_cli_importable()
    import devcake_cli.status as status_mod
    _fake_checkout(tmp_path)
    monkeypatch.setattr(status_mod, "ADMIN_URL", "http://127.0.0.1:9")   # closed port
    assert status_mod._fetch_health(tmp_path, timeout=0.5) is None
