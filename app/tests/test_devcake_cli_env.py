"""CAKE-177: ``.env`` auto-init + permission floor (ADR-0038 Decision 1).

Public seam: ``devcake_cli.envfile`` helpers used by ``devcake up``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CLI_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "cli",
    Path("/srv/cli"),
]


def _ensure_cli_importable() -> None:
    cli = next(p for p in _CLI_CANDIDATES if p.is_dir())
    if str(cli) not in sys.path:
        sys.path.insert(0, str(cli))


def test_auto_init_generates_missing_required_keys(tmp_path: Path, monkeypatch):
    _ensure_cli_importable()
    from devcake_cli import envfile

    for key in envfile.REQUIRED_BOOTSTRAP_KEYS:
        monkeypatch.delenv(key, raising=False)

    example = tmp_path / ".env.example"
    example.write_text(
        "ADMIN_USER=admin\n"
        "ADMIN_PASSWORD=\n"
        "REDIS_PASSWORD=\n"
        "DAGU_PASSWORD=\n"
        "OO_ROOT_PASSWORD=\n"
        "OO_INGEST_EMAIL=\n"
        "OO_INGEST_PASSWORD=\n"
        "GITEA_ADMIN_PASSWORD=\n"
    )
    env_path = tmp_path / ".env"
    envfile.seed_env_from_example(env_path, example)
    generated = envfile.auto_init_bootstrap(env_path)

    assert "ADMIN_PASSWORD" in generated
    assert "REDIS_PASSWORD" in generated
    assert "OO_INGEST_EMAIL" in generated
    data = envfile.parse_env_file(env_path)
    assert data["ADMIN_USER"] == "admin"  # preserved
    assert len(data["ADMIN_PASSWORD"]) >= 12
    assert "@" in data["OO_INGEST_EMAIL"]
    assert envfile.oo_password_ok(data["OO_ROOT_PASSWORD"])
    # mode 600 floor
    assert (env_path.stat().st_mode & 0o777) == 0o600


def test_auto_init_preserves_existing_strong_values(tmp_path: Path, monkeypatch):
    _ensure_cli_importable()
    from devcake_cli import envfile

    for key in envfile.REQUIRED_BOOTSTRAP_KEYS:
        monkeypatch.delenv(key, raising=False)

    env_path = tmp_path / ".env"
    env_path.write_text(
        "ADMIN_PASSWORD=AlreadyStrong1!\n"
        "REDIS_PASSWORD=AlreadyStrong2!\n"
        "DAGU_PASSWORD=AlreadyStrong3!\n"
        "OO_ROOT_PASSWORD=AlreadyStr4!\n"
        "OO_INGEST_PASSWORD=AlreadyStr5!\n"
        "GITEA_ADMIN_PASSWORD=AlreadyStr6!\n"
        "OO_INGEST_EMAIL=keep@example.com\n"
    )
    env_path.chmod(0o600)
    generated = envfile.auto_init_bootstrap(env_path)
    assert generated == []
    data = envfile.parse_env_file(env_path)
    assert data["ADMIN_PASSWORD"] == "AlreadyStrong1!"
    assert data["OO_INGEST_EMAIL"] == "keep@example.com"


def test_permission_floor_forces_600(tmp_path: Path):
    _ensure_cli_importable()
    from devcake_cli import envfile

    env_path = tmp_path / ".env"
    env_path.write_text("ADMIN_PASSWORD=AlreadyStrong1!\n")
    env_path.chmod(0o644)
    envfile.upsert_env_var("DOCKER_GID", "0", env_path)
    envfile.ensure_permission_floor(env_path)
    assert (env_path.stat().st_mode & 0o777) == 0o600
    assert envfile.parse_env_file(env_path)["DOCKER_GID"] == "0"
