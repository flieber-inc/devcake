"""The committed release pin (`VERSION`) and the changelog must agree —
`scripts/check_version_pin.py` is the CI gate; these pin its rules."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path("/srv/repo-scripts/check_version_pin.py")


def _mod():
    if not SCRIPT.exists():
        pytest.skip("scripts/ not mounted at /srv/repo-scripts")
    spec = importlib.util.spec_from_file_location("check_version_pin", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHANGELOG = """# Changelog

## Unreleased (pre-v1)

- **Changed — something.**

## v0.5.9 (2026-09-07)

- **Changed — newest release.**

## v0.5.8 (2026-09-06)

- **Added — older release.**
"""


def test_pin_matches_the_newest_changelog_release():
    m = _mod()
    assert m.newest_release(CHANGELOG) == "v0.5.9"
    assert m.check("v0.5.9\n", CHANGELOG) is None


def test_drift_in_either_direction_is_refused():
    m = _mod()
    assert "v0.5.8" in m.check("v0.5.8", CHANGELOG)          # forgot the bump
    assert "v0.6.0" in m.check("v0.6.0", CHANGELOG)          # forgot the section
    assert "release tag" in m.check("latest", CHANGELOG)     # not a release pin
    assert "no release section" in m.check("v0.5.9", "# Changelog\n")


def test_main_reads_the_checkout_root(tmp_path):
    m = _mod()
    (tmp_path / "VERSION").write_text("v0.5.9\n")
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    assert m.main(tmp_path) == 0
    (tmp_path / "VERSION").write_text("v0.5.8\n")
    assert m.main(tmp_path) == 1
