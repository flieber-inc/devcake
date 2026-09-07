"""`devcake up` tag resolution: the checkout's `VERSION` file is the pin,
the process environment overrides it for development builds, and `.env`
is written for lockstep but never read (docs/13, CONTRIBUTING "Cutting a
release")."""

from pathlib import Path

from test_devcake_cli_setup import _ensure_cli_importable


def _up():
    _ensure_cli_importable()
    from devcake_cli import up
    return up


def test_tag_prefers_process_env_then_version_then_latest(tmp_path: Path, monkeypatch):
    up = _up()
    env_path = tmp_path / ".env"
    env_path.write_text("DEVCAKE_TAG=v0.0.1\n")              # a hand-set, stale pin
    monkeypatch.delenv("DEVCAKE_TAG", raising=False)
    assert up.resolve_tag(env_path, tmp_path) == "latest"    # .env is never a source
    (tmp_path / "VERSION").write_text("v0.5.9\n")
    assert up.resolve_tag(env_path, tmp_path) == "v0.5.9"    # the checkout pins
    monkeypatch.setenv("DEVCAKE_TAG", "abc1234")
    assert up.resolve_tag(env_path, tmp_path) == "abc1234"   # a development override
    assert up.resolve_tag(env_path) == "abc1234"             # no checkout given: env still wins


def test_stale_env_pin_is_reported_not_obeyed(tmp_path: Path):
    up = _up()
    env_path = tmp_path / ".env"
    assert up.stale_env_tag(env_path, "v0.5.9") == ""        # no .env yet
    env_path.write_text("ADMIN_USER=a\nDEVCAKE_TAG=v0.5.3\n")
    assert up.stale_env_tag(env_path, "v0.5.9") == "v0.5.3"
    assert up.stale_env_tag(env_path, "v0.5.3") == ""        # agreeing is not stale


def test_version_pin_read_is_tolerant(tmp_path: Path):
    up = _up()
    assert up.read_version_pin(tmp_path) == ""               # no file
    (tmp_path / "VERSION").write_text("  v0.5.9  \n")
    assert up.read_version_pin(tmp_path) == "v0.5.9"         # whitespace stripped
