"""CAKE-176: console package + `devcake baker run` public seams.

Public seams under test:
- root pyproject.toml / cli/ packaging metadata (name, console script)
- `devcake_cli.main:main` argv dispatch → `dev_factory.watch.main`
- supervisor / devcake up / displace string contracts for the new entry

Does not re-test the baker conveyor (flock / keep-set / rotation) — those
stay in test_dev_factory.py.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

_CLI_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "cli",
    Path("/srv/cli"),
]

_PYPROJECT_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "pyproject.toml",
    Path("/srv/pyproject.toml"),
]

_SCRIPTS_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "scripts",
    Path("/srv/repo-scripts"),
]


def _pyproject_path() -> Path:
    path = next((p for p in _PYPROJECT_CANDIDATES if p.is_file()), None)
    assert path is not None, (
        "pyproject.toml missing — create at repo root, and bind "
        "/srv/pyproject.toml in the pytest runner"
    )
    return path


def _cli_root() -> Path:
    path = next((p for p in _CLI_CANDIDATES if p.is_dir()), None)
    assert path is not None, (
        "cli/ missing — create cli/devcake_cli/, and bind /srv/cli "
        "in the pytest runner"
    )
    return path


def _scripts_root() -> Path:
    path = next((p for p in _SCRIPTS_CANDIDATES if p.is_dir()), None)
    assert path is not None, "scripts/ missing — bind scripts → /srv/repo-scripts"
    return path


def _ensure_cli_importable() -> None:
    cli = _cli_root()
    if str(cli) not in sys.path:
        sys.path.insert(0, str(cli))
    scripts = _scripts_root()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))


def test_pyproject_declares_devcake_cli_console_script():
    """Packaging: name=devcake-cli, console script → devcake_cli.main:main."""
    data = tomllib.loads(_pyproject_path().read_text())
    assert data["project"]["name"] == "devcake-cli"
    scripts = data["project"]["scripts"]
    assert scripts["devcake"] == "devcake_cli.main:main"
    # Package lives under cli/ (not app/), so checkout PYTHONPATH=…:app
    # cannot shadow app/devcake.
    pkg_dir = data["tool"]["setuptools"]["package-dir"]
    assert pkg_dir[""] == "cli"
    where = data["tool"]["setuptools"]["packages"]["find"]["where"]
    assert where == ["cli"] or where == "cli"
    assert (_cli_root() / "devcake_cli").is_dir()


def test_cli_package_layout_exists():
    """cli/devcake_cli exposes main + baker modules (ADR-0038 Decision 3)."""
    root = _cli_root() / "devcake_cli"
    assert (root / "__init__.py").is_file()
    assert (root / "__main__.py").is_file()
    assert (root / "main.py").is_file()
    assert (root / "baker.py").is_file()


def test_baker_run_dispatches_to_watch_main(monkeypatch):
    """`devcake baker run` resolves to the same watch.main as python -m dev_factory."""
    _ensure_cli_importable()
    import dev_factory.watch as watch
    import devcake_cli.main as cli_main

    called: list[int] = []

    def _fake_main() -> int:
        called.append(1)
        return 0

    # baker.run imports watch.main at call time — patch the module attribute.
    monkeypatch.setattr(watch, "main", _fake_main)

    rc = cli_main.main(["baker", "run"])
    assert rc == 0
    assert called == [1]


def test_phase1c_verbs_are_registered_and_bake_still_stubbed():
    """Phase 1c implements up/down/status/doctor/setup; bake remains exit 2."""
    _ensure_cli_importable()
    import devcake_cli.main as cli_main

    assert cli_main.main([]) == 2
    assert cli_main.main(["bake"]) == 2
    # Implemented verbs must not return the old "not implemented" usage stub.
    assert cli_main.main(["up", "--help"]) == 0
    assert cli_main.main(["down", "--help"]) == 0
    assert cli_main.main(["status", "--help"]) == 0
    assert cli_main.main(["doctor", "--help"]) == 0
    assert cli_main.main(["setup", "--help"]) == 0


def test_deprecated_dev_factory_module_entry_still_imports():
    """ADR Decision 5: python -m dev_factory remains import-compatible."""
    scripts = _scripts_root()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import dev_factory
    from dev_factory.watch import main as watch_main

    assert callable(watch_main)
    assert hasattr(dev_factory, "load_keep_set")


def _baker_host_sh_path() -> Path:
    candidates = [
        Path(__file__).resolve().parents[2] / "scripts" / "lib" / "baker_host.sh",
        Path("/srv/repo-scripts/lib/baker_host.sh"),
    ]
    path = next((p for p in candidates if p.is_file()), None)
    assert path is not None, "baker_host.sh missing"
    return path


def test_cmdline_matcher_accepts_legacy_and_cli_entries(tmp_path):
    """Orphan displace must match both -m dev_factory and `devcake baker run`."""
    helper = _baker_host_sh_path()
    driver = tmp_path / "driver.sh"
    driver.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'source "{helper}"\n'
        "fail=0\n"
        'devcake_baker_cmdline_is_module "python3 -m dev_factory" || fail=1\n'
        'devcake_baker_cmdline_is_module "/usr/bin/python3 -m dev_factory" || fail=1\n'
        'devcake_baker_cmdline_is_module "python3 -m dev_factory.watch" || fail=1\n'
        'devcake_baker_cmdline_is_module "/home/u/.local/bin/devcake baker run" || fail=1\n'
        'devcake_baker_cmdline_is_module "devcake baker run" || fail=1\n'
        'devcake_baker_cmdline_is_module "/opt/venv/bin/python /opt/bin/devcake baker run" || fail=1\n'
        # Must NOT match the respawn supervisor or unrelated commands.
        'if devcake_baker_cmdline_is_module "bash baker_respawn.sh /repo /.factory"; then fail=1; fi\n'
        'if devcake_baker_cmdline_is_module "python3 -m pytest"; then fail=1; fi\n'
        'if [[ "$fail" -eq 0 ]]; then echo OK; else echo FAIL; exit 1; fi\n'
    )
    driver.chmod(0o700)
    result = subprocess.run(
        ["bash", str(driver)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK" in result.stdout


# ── degraded (flock respawn) supervisor handoff ─────────────────────────────
# 2026-09-01: `devcake up` on WSL2 left the host baker dead. The old loop's
# lock fd was inherited by its children (the baker, the backoff sleep), so a
# stopped supervisor's orphans kept the lock; the install slept a fixed 0.3s
# and the new supervisor gave up on a busy lock at once ("died at launch").
# Seams: scripts/lib/baker_respawn.sh (the loop) and the baker_host.sh
# install / stop functions `devcake up` runs. Real processes, no docker.


def _factory_sandbox(tmp_path: Path) -> dict[str, Path]:
    """Throwaway repo layout for the supervisor scripts: `scripts/` links to
    the real tree, `.factory/` is empty, and a `devcake` on PATH whose
    `baker run` idles until SIGTERM (no conveyor, no docker)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "scripts").symlink_to(_scripts_root(), target_is_directory=True)
    factory = repo / ".factory"
    factory.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "devcake"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        '[[ "${1:-} ${2:-}" == "baker run" ]] || exit 64\n'
        "echo fake-baker: up\n"
        "trap 'exit 0' TERM\n"
        "while :; do sleep 0.2; done\n"
    )
    fake.chmod(0o700)
    return {"repo": repo, "factory": factory, "bin": bin_dir,
            "log": factory / "watch.log", "pidfile": factory / "watch.pid"}


def _run_driver(tmp_path: Path, sandbox: dict[str, Path], body: str):
    driver = tmp_path / "driver.sh"
    driver.write_text(
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        f'export PATH="{sandbox["bin"]}:$PATH"\n'
        f'source "{_baker_host_sh_path()}"\n'
        f'REPO="{sandbox["repo"]}"; FACTORY="{sandbox["factory"]}"\n'
        f'LOG="{sandbox["log"]}"; PIDFILE="{sandbox["pidfile"]}"\n'
        'LOCK="$FACTORY/watch.respawn.lock"\n'
        # a live baker pid in the pidfile, or failure after ~6s
        "wait_baker() { local i b; for i in $(seq 1 60); do sleep 0.1;\n"
        '  b="$(cat "$PIDFILE" 2>/dev/null || true)";\n'
        '  [[ -n "$b" ]] && kill -0 "$b" 2>/dev/null && { echo "$b"; return 0; }; done; return 1; }\n'
        + body
    )
    driver.chmod(0o700)
    return subprocess.run(["bash", str(driver)], capture_output=True,
                          text=True, cwd=str(tmp_path), timeout=90)


def test_respawn_lock_is_not_inherited_by_the_baker_child(tmp_path):
    """The supervisor's lock must die with the supervisor: with the baker
    still running as an orphan, the lock is free for a successor."""
    sb = _factory_sandbox(tmp_path)
    result = _run_driver(tmp_path, sb, (
        'bash "$REPO/scripts/lib/baker_respawn.sh" "$REPO" "$FACTORY" "$LOG" "$PIDFILE" >>"$LOG" 2>&1 &\n'
        "sup=$!\n"
        'baker="$(wait_baker)" || { echo NO_BAKER; cat "$LOG"; exit 1; }\n'
        'kill -9 "$sup"; sleep 0.3\n'
        'kill -0 "$baker" 2>/dev/null || { echo BAKER_DIED_WITH_SUPERVISOR; exit 1; }\n'
        'if flock -n "$LOCK" true; then echo LOCK_FREE; else echo LOCK_HELD; fi\n'
        'kill "$baker" 2>/dev/null; true\n'
    ))
    assert result.returncode == 0, result.stderr + result.stdout
    assert "LOCK_FREE" in result.stdout, result.stdout


def test_respawn_install_outlasts_a_predecessor_still_releasing_the_lock(tmp_path):
    """`devcake up` stops the previous supervisor and launches a successor.
    A predecessor may keep its lock for a moment after SIGTERM (winding
    down); the install must wait for it to exit rather than launch into the
    held lock and report the successor dead at launch."""
    sb = _factory_sandbox(tmp_path)
    result = _run_driver(tmp_path, sb, (
        # predecessor: holds the lock, exits ~1.4s after SIGTERM
        "bash -c 'exec 9>\"$1\"; flock 9; trap \"sleep 1.2; exit 0\" TERM; "
        "while :; do sleep 0.2; done' _ \"$LOCK\" &\n"
        "old=$!\n"
        'echo "$old" >"$FACTORY/watch.respawn.pid"\n'
        "sleep 0.4\n"
        'flock -n "$LOCK" true && { echo PREDECESSOR_NOT_HOLDING; exit 1; }\n'
        'if devcake_baker_respawn_install "$REPO" "$FACTORY" "$LOG" "$PIDFILE"; then '
        "echo INSTALL_OK; else echo INSTALL_FAILED; fi\n"
        'kill -0 "$old" 2>/dev/null && echo PREDECESSOR_STILL_ALIVE\n'
        'sup="$(cat "$FACTORY/watch.respawn.pid" 2>/dev/null || true)"; '
        'b="$(cat "$PIDFILE" 2>/dev/null || true)"\n'
        'kill "$sup" 2>/dev/null; sleep 0.2; kill "$b" "$old" 2>/dev/null; true\n'
    ))
    assert result.returncode == 0, result.stderr + result.stdout
    assert "INSTALL_OK" in result.stdout, result.stdout + result.stderr
    assert "supervised by flock respawn loop" in result.stdout, result.stdout
    assert "PREDECESSOR_STILL_ALIVE" not in result.stdout, result.stdout


def test_stop_respawn_supervisor_escalates_when_sigterm_is_ignored(tmp_path):
    """Stopping a supervisor that ignores SIGTERM must still end with it
    gone (SIGKILL past the wait budget) and its pidfile removed — the
    handoff never proceeds against a predecessor that is still alive."""
    sb = _factory_sandbox(tmp_path)
    result = _run_driver(tmp_path, sb, (
        "bash -c 'trap \"\" TERM; while :; do sleep 0.2; done' &\n"
        "old=$!\n"
        'echo "$old" >"$FACTORY/watch.respawn.pid"\n'
        "sleep 0.2\n"
        'DEVCAKE_BAKER_EXIT_WAIT=1 devcake_baker_stop_respawn_supervisor "$FACTORY"\n'
        "sleep 0.2\n"
        'if kill -0 "$old" 2>/dev/null; then echo STILL_ALIVE; kill -9 "$old"; else echo GONE; fi\n'
        '[[ -f "$FACTORY/watch.respawn.pid" ]] && echo PIDFILE_LEFT; true\n'
    ))
    assert result.returncode == 0, result.stderr + result.stdout
    assert "GONE" in result.stdout, result.stdout + result.stderr
    assert "PIDFILE_LEFT" not in result.stdout, result.stdout
