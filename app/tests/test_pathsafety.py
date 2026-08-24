"""Unit tests for ``devcake.pathsafety.confined`` — path-component hygiene
plus resolve+relative_to confinement under a trusted base."""

from __future__ import annotations

from pathlib import Path

import pytest

from devcake.pathsafety import confined


def test_confined_rejects_dot_dot(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe path component"):
        confined(tmp_path, "..")


def test_confined_rejects_dot(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe path component"):
        confined(tmp_path, ".")


def test_confined_rejects_empty(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe path component"):
        confined(tmp_path, "")


def test_confined_rejects_slash_in_component(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe path component"):
        confined(tmp_path, "a/b")


def test_confined_rejects_backslash_in_component(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe path component"):
        confined(tmp_path, "a\\b")


def test_confined_rejects_null_byte(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe path component"):
        confined(tmp_path, "a\x00b")


def test_confined_accepts_normal_single_component(tmp_path: Path) -> None:
    base = tmp_path / "secrets"
    base.mkdir()
    path = confined(base, "normal.json")
    assert path == (base / "normal.json").resolve()
    path.relative_to(base.resolve())  # does not raise


def test_confined_multi_part_stays_under_base(tmp_path: Path) -> None:
    base = tmp_path / "secrets"
    base.mkdir()
    path = confined(base, "connections", "repo-main.json")
    assert path == (base / "connections" / "repo-main.json").resolve()
    path.relative_to(base.resolve())


def test_confined_allows_spaces_in_component(tmp_path: Path) -> None:
    """Profile names may contain spaces; confined must not reject them."""
    base = tmp_path / "profiles"
    base.mkdir()
    path = confined(base, "My Profile.json")
    assert path.name == "My Profile.json"
    path.relative_to(base.resolve())


def test_confined_escape_cannot_leave_base(tmp_path: Path) -> None:
    """Even a crafted multi-part join that would walk out must fail closed.

    Component checks already reject ``..``; this asserts the resolve+
    relative_to belt still holds the result under ``base``.
    """
    base = tmp_path / "jail"
    base.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")
    # Direct ``..`` component is rejected before join.
    with pytest.raises(ValueError):
        confined(base, "..", "outside.txt")
    assert not (base / "outside.txt").exists()
    # Happy path stays inside.
    inside = confined(base, "ok.txt")
    assert str(inside).startswith(str(base.resolve()))


def test_confined_multi_part_normpath_belt_under_constant_base(
        tmp_path: Path) -> None:
    """Multi-part join under a constant base stays inside after the
    normalize-then-prefix belt (CAKE-140) and resolve."""
    import os

    base = tmp_path / "prompt_templates"
    base.mkdir()
    path = confined(base, "EXECUTE", "terse.yaml")
    base_norm = os.path.normpath(str(base))
    assert os.path.normpath(str(path)).startswith(base_norm + os.sep)
    assert path == (base / "EXECUTE" / "terse.yaml").resolve()


def test_confined_refuses_symlink_escape_after_normpath_belt(
        tmp_path: Path) -> None:
    """A symlink inside the jail that points outside must still raise;
    the outside sentinel must remain untouched (CAKE-140).

    The normalize-then-prefix check is lexical (does not follow links);
    resolve()+relative_to remains the symlink belt.
    """
    jail = tmp_path / "jail"
    jail.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    sentinel = outside_dir / "secret.txt"
    sentinel.write_text("keep-me\n")
    link = jail / "escape"
    link.symlink_to(outside_dir)

    with pytest.raises(ValueError):
        confined(jail, "escape", "secret.txt")

    assert sentinel.exists()
    assert sentinel.read_text() == "keep-me\n"
