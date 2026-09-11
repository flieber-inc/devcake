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


def _no_apparmor(monkeypatch):
    """Hermetic: never ask this developer's daemon (docker info, a throwaway
    container) from the CLI tests."""
    import devcake_cli.up as up_mod
    from devcake_cli import doctor
    monkeypatch.setattr(up_mod, "apparmor_facts", lambda **_: doctor.ApparmorFacts(
        enabled=False, loaded=None, installed=False, current=None, parser=False,
        compiles=None, applies=None))
    monkeypatch.setattr(up_mod, "_dagu_backup", lambda *a, **k: None)


def test_up_dry_run_no_mutation(monkeypatch, tmp_path, capsys):
    _ensure_cli_importable()
    import devcake_cli.main as cli_main
    import devcake_cli.up as up_mod

    sock = _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_SOCK", str(sock))
    _no_apparmor(monkeypatch)

    # Point discovery at the fake stack_env helpers via real bash source.
    rc = cli_main.main(["--json", "up", "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err + captured.out
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["dry_run"] is True
    assert payload["docker_gid"] == "4242"
    assert payload["devcake_apparmor_profile"] == "docker-default"
    assert payload["bake"] is False
    # dry-run must not create .env
    assert not (tmp_path / ".env").exists()


def test_up_dry_run_bake_plan(monkeypatch, tmp_path, capsys):
    _ensure_cli_importable()
    import devcake_cli.main as cli_main

    sock = _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_SOCK", str(sock))
    _no_apparmor(monkeypatch)

    rc = cli_main.main(["up", "--dry-run", "--bake", "app", "admin"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err + captured.out
    text = captured.out + captured.err
    assert "docker buildx bake app admin" in text
    assert "would upsert DOCKER_GID=4242" in text


def test_up_replaces_the_baker_before_the_app_is_recreated(monkeypatch, tmp_path):
    """The app publishes its one-shot bake order at boot; a baker replaced
    only afterwards lets the outgoing one claim it (field, 2026-09). The
    detached baker is replaced before compose up; the foreground variant
    execs and so stays last."""
    _ensure_cli_importable()
    import devcake_cli.up as up_mod

    sock = _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_SOCK", str(sock))
    _no_apparmor(monkeypatch)
    order = []
    for name in ("_bake", "_compose_up", "_health_gate", "_hello_smoke", "_start_baker"):
        monkeypatch.setattr(up_mod, name,
                            lambda *a, _n=name, **k: order.append(_n))
    assert up_mod.run_up(up_mod.UpOptions(bake=True), repo=tmp_path) == 0
    assert order == ["_bake", "_start_baker", "_compose_up", "_health_gate",
                     "_hello_smoke"]
    order.clear()
    assert up_mod.run_up(up_mod.UpOptions(bake=True, foreground_baker=True),
                         repo=tmp_path) == 0
    assert order == ["_bake", "_compose_up", "_health_gate", "_hello_smoke",
                     "_start_baker"]


def test_status_reports_harness_pins_and_the_lost_order_remedy(monkeypatch, tmp_path, capsys):
    """Every pin waiting while the baker is idle with no job means the app's
    bake order was lost: `devcake status` names the remedy."""
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
        "harness_pins": {"templates": {
            "grok-build": {"cli_version": "1.0.13", "ok": False, "state": "waiting",
                           "reason": "no receipt for grok-build 1.0.13"},
            "claude-code": {"cli_version": "2.1.258", "ok": True, "state": "ready",
                            "reason": ""}}},
        "bake_status": {"state": "ready", "jobs": [], "baker_alive": True},
    }
    monkeypatch.setattr(status_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(status_mod, "_fetch_health",
                        lambda root, **kw: (health, None))
    rc = cli_main.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "  claude-code 2.1.258: staffed" in out
    assert "  grok-build 1.0.13: waiting — no receipt for grok-build 1.0.13" in out
    assert "the baker is idle with no bake order in flight" in out
    # a baking baker is not a lost order
    health["bake_status"] = {"state": "baking", "jobs": ["grok-build@1.0.13"]}
    cli_main.main(["status"])
    assert "no bake order in flight" not in capsys.readouterr().out
    # the last prune's outcome, with its time
    health["bake_status"] = {"state": "ready", "jobs": [], "baker_alive": True,
                             "prune": {"removed": ["devcake/dev-x:v1-1.0", "devcake/dev-y:v1"],
                                       "kept": 2, "detail": "", "at": "2026-09-08T18:07:12+00:00"}}
    health["harness_pins"]["templates"]["grok-build"]["ok"] = True
    cli_main.main(["status"])
    assert "  last prune (18:07 UTC): removed 2 image(s)" in capsys.readouterr().out
    rc = cli_main.main(["status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["harness_pins"] == health["harness_pins"]["templates"]
    assert payload["bake_status"] == health["bake_status"]


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
    monkeypatch.setattr(status_mod, "_fetch_health",
                        lambda root, **kw: (health, None))
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
    monkeypatch.setattr(
        status_mod, "_fetch_health",
        lambda root, **kw: (None, "the admin proxy at http://127.0.0.1:8080 "
                            "could not be reached (URLError: [Errno 111] "
                            "Connection refused) — stack down?"))
    assert cli_main.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "pmo_budget: unavailable (the admin proxy at http://127.0.0.1:8080" in out
    cli_main.main(["status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["health_reachable"] is False and payload["pmo_budget"] is None
    assert payload["pmo_rate_limited"] is None
    assert "stack down" in payload["health_error"]


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve_once(raw: bytes) -> int:
    """A one-shot loopback server that answers any request with `raw`."""
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def _run():
        conn, _ = srv.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(raw)
        srv.close()
    threading.Thread(target=_run, daemon=True).start()
    return srv.getsockname()[1]


def _env_with_admin(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("ADMIN_USER=admin\nADMIN_PASSWORD=p:ä\n")


def test_fetch_health_never_raises(monkeypatch, tmp_path):
    """A stack that is down, a wrong password, a truncated body: each is a
    reason string, never a traceback — and the loopback call ignores any
    http_proxy in the environment (the admin password must not leave the
    host)."""
    _ensure_cli_importable()
    import devcake_cli.status as status_mod
    _fake_checkout(tmp_path)
    # no .env → the missing-credentials reason, no network at all
    body, why = status_mod._fetch_health(tmp_path, timeout=0.5)
    assert body is None and "ADMIN_USER" in why
    _env_with_admin(tmp_path)
    # a proxy that WOULD answer: an env-honouring opener returns its body
    # (and hands it the admin password); the loopback call must never see it
    proxy = _serve_once(b"HTTP/1.0 200 OK\r\nContent-Length: 15\r\n\r\n{\"via\":\"proxy\"}")
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy}")
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy}")
    monkeypatch.setattr(status_mod, "ADMIN_URL", f"http://127.0.0.1:{_free_port()}")
    body, why = status_mod._fetch_health(tmp_path, timeout=0.5)
    assert body is None and "could not be reached" in why and "stack down" in why
    monkeypatch.delenv("http_proxy")
    monkeypatch.delenv("HTTP_PROXY")
    port = _serve_once(b"HTTP/1.0 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
    monkeypatch.setattr(status_mod, "ADMIN_URL", f"http://127.0.0.1:{port}")
    body, why = status_mod._fetch_health(tmp_path, timeout=2)
    assert body is None and "HTTP 401" in why and "ADMIN_PASSWORD" in why
    port = _serve_once(b"HTTP/1.0 200 OK\r\nContent-Length: 100\r\n\r\n{\"ok\":")
    monkeypatch.setattr(status_mod, "ADMIN_URL", f"http://127.0.0.1:{port}")
    body, why = status_mod._fetch_health(tmp_path, timeout=2)
    assert body is None and "IncompleteRead" in why
    port = _serve_once(b"HTTP/1.0 200 OK\r\nContent-Length: 16\r\n\r\n{\"pmo_budget\":1}")
    monkeypatch.setattr(status_mod, "ADMIN_URL", f"http://127.0.0.1:{port}")
    body, why = status_mod._fetch_health(tmp_path, timeout=2)
    assert body == {"pmo_budget": 1} and why is None


def test_up_derives_the_apparmor_profile_and_never_refuses(monkeypatch, tmp_path, capsys):
    """docs/13: `devcake up` writes DEVCAKE_APPARMOR_PROFILE from what the
    host says — devcake-nested when loaded, docker-default with the two
    install commands printed (never run) when AppArmor is active without
    it. A red host still brings the stack up."""
    _ensure_cli_importable()
    import devcake_cli.up as up_mod
    from devcake_cli import doctor

    sock = _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_SOCK", str(sock))
    for name in ("_bake", "_compose_up", "_health_gate", "_hello_smoke", "_start_baker",
                 "_dagu_backup"):
        monkeypatch.setattr(up_mod, name, lambda *a, **k: None)

    def host(**kw):
        base = dict(enabled=True, loaded=None, installed=False, current=None, parser=True,
                    compiles=None, applies=None)
        base.update(kw)
        monkeypatch.setattr(up_mod, "apparmor_facts",
                            lambda **_: doctor.ApparmorFacts(**base))

    host(enabled=False)
    assert up_mod.run_up(up_mod.UpOptions(), repo=tmp_path) == 0
    env = (tmp_path / ".env").read_text()
    assert "DEVCAKE_APPARMOR_PROFILE=docker-default" in env
    assert "WARNING" not in capsys.readouterr().out

    host(enabled=True, installed=False)
    assert up_mod.run_up(up_mod.UpOptions(), repo=tmp_path) == 0
    out = capsys.readouterr().out
    assert "DEVCAKE_APPARMOR_PROFILE=docker-default" in (tmp_path / ".env").read_text()
    assert "nested containers inside Dev containers are unavailable" in out
    assert "printed only; this CLI will not run it" in out
    assert "sudo apparmor_parser -r /etc/apparmor.d/devcake-nested" in out

    host(enabled=True, loaded=True, installed=True, current=True)
    assert up_mod.run_up(up_mod.UpOptions(), repo=tmp_path) == 0
    out = capsys.readouterr().out
    assert "DEVCAKE_APPARMOR_PROFILE=devcake-nested" in (tmp_path / ".env").read_text()
    assert "WARNING" not in out

    # installed but the daemon refuses it (older parser, load failed):
    # docker-default, with the cause named
    host(enabled=True, loaded=None, installed=True, compiles=False, applies=False)
    assert up_mod.run_up(up_mod.UpOptions(), repo=tmp_path) == 0
    out = capsys.readouterr().out
    assert "DEVCAKE_APPARMOR_PROFILE=docker-default" in (tmp_path / ".env").read_text()
    assert "rejected by this host's apparmor_parser" in out and "4.0 or newer" in out
    # ... and when the host's parser compiles it fine, the file was simply
    # never loaded (or unloaded): say that, not "old parser"
    host(enabled=True, loaded=None, installed=True, compiles=True, applies=False)
    assert up_mod.run_up(up_mod.UpOptions(), repo=tmp_path) == 0
    out = capsys.readouterr().out
    assert "installed but not loaded by the kernel" in out and "4.0" not in out

    host(enabled=True, loaded=True, installed=True, current=False)
    assert up_mod.run_up(up_mod.UpOptions(), repo=tmp_path) == 0
    out = capsys.readouterr().out
    assert "DEVCAKE_APPARMOR_PROFILE=devcake-nested" in (tmp_path / ".env").read_text()
    assert "differs from" in out and "apparmor_parser -r" in out

    # dry-run reports the derivation without writing
    (tmp_path / ".env").unlink()
    host(enabled=True, installed=False)
    assert up_mod.run_up(up_mod.UpOptions(dry_run=True), repo=tmp_path) == 0
    out = capsys.readouterr().out
    assert "would upsert DEVCAKE_APPARMOR_PROFILE=docker-default" in out
    assert not (tmp_path / ".env").exists()


def test_up_release_prints_the_dagu_backup_line_only_on_a_moved_pin():
    _ensure_cli_importable()
    import devcake_cli.up as up_mod
    compose = "  dagu:\n    image: ghcr.io/dagucloud/dagu:2.16.3@sha256:abc\n"
    assert up_mod.dagu_pin_moved(compose, "ghcr.io/dagucloud/dagu:2.13.0") == ("2.13.0", "2.16.3")
    assert up_mod.dagu_pin_moved(compose, "ghcr.io/dagucloud/dagu:2.16.3") is None
    assert up_mod.dagu_pin_moved(compose, "") is None
    assert up_mod.dagu_pin_moved("", "ghcr.io/dagucloud/dagu:2.13.0") is None


def test_up_archives_dagu_state_before_a_re_pinned_dagu_starts(monkeypatch, tmp_path, capsys):
    """A re-pinned Dagu migrates its state store on first start; the
    archive taken first is the rollback path — on every `up`, for a
    running OR stopped stack, written 0600 in a 0700 directory with dagu
    stopped first. Dry-run prints the command; a failed archive warns and
    never blocks the bring-up; an unchanged pin says nothing."""
    _ensure_cli_importable()
    import devcake_cli.up as up_mod
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  dagu:\n    image: ghcr.io/dagucloud/dagu:2.16.3@sha256:abc\n")
    monkeypatch.setattr(up_mod, "_running_dagu_image", lambda repo: "ghcr.io/dagucloud/dagu:2.13.0")
    monkeypatch.setattr(up_mod, "_dagu_volume", lambda repo: "devcake_dagu_data")
    monkeypatch.setattr(up_mod, "_dagu_pin_seen_locally", lambda repo, text: False)
    up_mod._dagu_backup(tmp_path, as_json=False, dry_run=True)
    out = capsys.readouterr().out
    assert "re-pins Dagu 2.13.0 → 2.16.3" in out and "Would archive" in out
    assert "devcake_dagu_data:/from:ro" in out and "umask 077" in out
    assert "dagu_data-from-2.13.0-" in out
    assert not (tmp_path / ".factory" / "backups").exists()

    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[:2] == ["docker", "run"]:
            dest = tmp_path / ".factory" / "backups"
            name = cmd[-1].split("/to/", 1)[1].split(" ", 1)[0]
            (dest / name).write_bytes(b"x" * 2048)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(up_mod.subprocess, "run", fake_run)
    up_mod._dagu_backup(tmp_path, as_json=False)
    out = capsys.readouterr().out
    assert "Dagu state archived (2 KiB, 0600)" in out
    assert calls[0][:4] == ["docker", "compose", "stop", "dagu"]      # a quiet copy
    assert calls[1][:2] == ["docker", "run"] and "--network" in calls[1]
    assert calls[2][:4] == ["docker", "compose", "start", "dagu"]     # never left down
    backups = tmp_path / ".factory" / "backups"
    assert oct(backups.stat().st_mode & 0o777) == "0o700"
    archive = next(backups.glob("dagu_data-*.tgz"))
    assert oct(archive.stat().st_mode & 0o777) == "0o600"

    # the stack is down: compose knows no container. The pinned tag was
    # never pulled here → the previous version is unknown → archived
    monkeypatch.setattr(up_mod, "_running_dagu_image", lambda repo: "")
    calls.clear()
    up_mod._dagu_backup(tmp_path, as_json=False)
    out = capsys.readouterr().out
    assert "never run here" in out and "archived" in out and calls
    assert any("dagu_data-from-unknown-" in part for part in calls[1][-1:])
    # ... but a routine down/up with the pin already pulled says nothing
    monkeypatch.setattr(up_mod, "_dagu_pin_seen_locally", lambda repo, text: True)
    calls.clear()
    up_mod._dagu_backup(tmp_path, as_json=False)
    assert capsys.readouterr().out == "" and not calls
    monkeypatch.setattr(up_mod, "_dagu_pin_seen_locally", lambda repo, text: False)

    # ... and without a volume there is nothing to archive, silently
    monkeypatch.setattr(up_mod, "_dagu_volume", lambda repo: None)
    up_mod._dagu_backup(tmp_path, as_json=False)
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(up_mod, "_dagu_volume", lambda repo: "devcake_dagu_data")
    monkeypatch.setattr(up_mod, "_running_dagu_image", lambda repo: "ghcr.io/dagucloud/dagu:2.13.0")

    failing = []

    def fail_run(cmd, **kw):
        failing.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1, "", "no space")
    monkeypatch.setattr(up_mod.subprocess, "run", fail_run)
    up_mod._dagu_backup(tmp_path, as_json=False)
    out = capsys.readouterr().out
    assert "WARNING: the Dagu archive failed (no space)" in out and "continuing" in out
    assert failing[-1][:4] == ["docker", "compose", "start", "dagu"]  # restarted even so

    # same pin → nothing said
    monkeypatch.setattr(up_mod, "_running_dagu_image", lambda repo: "ghcr.io/dagucloud/dagu:2.16.3")
    up_mod._dagu_backup(tmp_path, as_json=False)
    assert capsys.readouterr().out == ""


def test_status_says_whether_devs_can_run_containers():
    """docs/11 `bake_status.nested`: the newest nested-engine receipt, one
    line — green, or the first red step in plain words."""
    _ensure_cli_importable()
    from devcake_cli.status import harness_lines
    health = {"harness_pins": {"templates": {}}, "bake_status": {
        "state": "ready", "jobs": [], "baker_alive": True,
        "nested": {"rig_ok": True, "measured_at": "20260910T235021Z",
                   "first_red": ""}}}
    lines = harness_lines(health)
    assert "  nested engine: ok — Devs can run containers (measured 2026-09-10 23:50 UTC)" in lines
    health["bake_status"]["nested"]["compose_ok"] = True
    assert any("containers, compose too (measured" in line for line in harness_lines(health))
    health["bake_status"]["nested"]["compose_ok"] = False
    assert any("docker compose is not working" in line for line in harness_lines(health))
    health["bake_status"]["nested"] = {
        "rig_ok": False, "measured_at": "20260910T220000Z",
        "first_red": "the engine cannot create a user namespace (uid_map: EPERM)"}
    lines = harness_lines(health)
    assert ("  nested engine: unavailable — the engine cannot create a user "
            "namespace (uid_map: EPERM) (measured 2026-09-10 22:00 UTC)") in lines
    del health["bake_status"]["nested"]
    assert not any("nested engine" in line for line in harness_lines(health))



def test_dagu_pin_seen_locally_asks_for_the_digest_not_the_tag(monkeypatch, tmp_path):
    """Docker stores no tag for a `tag@digest` pull, only the digest — a
    tag lookup could never fire (measured on the reference rig); the
    discriminator inspects the digest the compose file pins."""
    _ensure_cli_importable()
    import devcake_cli.up as up_mod
    compose = ("  dagu:\n    image: ghcr.io/dagucloud/dagu:2.16.3@sha256:"
               + "6" * 64 + "\n")
    seen = []

    def fake_run(cmd, **kw):
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "sha256:abc\n", "")
    monkeypatch.setattr(up_mod.subprocess, "run", fake_run)
    assert up_mod._dagu_pin_seen_locally(tmp_path, compose)
    assert seen[0][:3] == ["docker", "image", "inspect"]
    assert seen[0][3] == "ghcr.io/dagucloud/dagu@sha256:" + "6" * 64
    monkeypatch.setattr(up_mod.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "No such image"))
    assert not up_mod._dagu_pin_seen_locally(tmp_path, compose)
    assert not up_mod._dagu_pin_seen_locally(tmp_path, "image: ghcr.io/dagucloud/dagu:2.16.3\n")


def test_archive_rotation_never_evicts_the_newest_versioned_archive(tmp_path):
    _ensure_cli_importable()
    import os
    import time as _t
    import devcake_cli.up as up_mod
    names = ["dagu_data-from-2.13.0-20260901T000000Z.tgz",
             "dagu_data-from-unknown-20260902T000000Z.tgz",
             "dagu_data-from-unknown-20260903T000000Z.tgz",
             "dagu_data-from-unknown-20260904T000000Z.tgz",
             "dagu_data-from-unknown-20260905T000000Z.tgz"]
    for i, n in enumerate(names):
        p = tmp_path / n
        p.write_bytes(b"x")
        os.utime(p, (1000 + i, 1000 + i))
    up_mod._rotate_archives(tmp_path)
    left = sorted(p.name for p in tmp_path.glob("*.tgz"))
    assert "dagu_data-from-2.13.0-20260901T000000Z.tgz" in left
    assert len(left) == 4 and "dagu_data-from-unknown-20260902T000000Z.tgz" not in left


def test_compose_project_name_comes_from_the_env_then_the_file(monkeypatch, tmp_path):
    _ensure_cli_importable()
    import devcake_cli.up as up_mod
    (tmp_path / "docker-compose.yml").write_text("name: devcake\nservices: {}\n")
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    assert up_mod._compose_project(tmp_path) == "devcake"
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "other")
    assert up_mod._compose_project(tmp_path) == "other"
