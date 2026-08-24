"""Store-level traversal refusal when mission_type / dev_type ride as
confined PARTS under constant roots (CAKE-140).

After #324, save/delete already called confined, but built the base from
user-supplied type via _dir/_dev_dir. Hostile types must be refused at the
store layer; a sentinel outside the template tree must stay untouched.
"""

from __future__ import annotations

import re

import pytest


def _tpl(monkeypatch, tmp_path):
    import devcake.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")
    from devcake.prompts import templates
    return templates


_HOSTILE_TYPES = ("..", "foo/bar", "../x", "a\\b")


@pytest.mark.parametrize("bad", _HOSTILE_TYPES)
def test_save_template_refuses_hostile_mission_type(
        monkeypatch, tmp_path, bad):
    t = _tpl(monkeypatch, tmp_path)
    (tmp_path / "config" / "prompt_templates").mkdir(parents=True)
    sentinel = tmp_path / "config" / "outside.yaml"
    sentinel.write_text("keep: me\n")

    with pytest.raises(ValueError):
        t.save_template(bad, "terse", "just {key} on {branch}")

    assert sentinel.exists()
    assert "keep: me" in sentinel.read_text()


@pytest.mark.parametrize("bad", _HOSTILE_TYPES)
def test_delete_template_refuses_hostile_mission_type(
        monkeypatch, tmp_path, bad):
    t = _tpl(monkeypatch, tmp_path)
    (tmp_path / "config" / "prompt_templates").mkdir(parents=True)
    sentinel = tmp_path / "config" / "outside.yaml"
    sentinel.write_text("keep: me\n")

    with pytest.raises(ValueError):
        t.delete_template(bad, "terse")

    assert sentinel.exists()
    assert "keep: me" in sentinel.read_text()


@pytest.mark.parametrize("bad", _HOSTILE_TYPES)
def test_save_devtype_prompt_refuses_hostile_dev_type(
        monkeypatch, tmp_path, bad):
    t = _tpl(monkeypatch, tmp_path)
    (tmp_path / "config" / "devtype_prompt_templates").mkdir(parents=True)
    sentinel = tmp_path / "config" / "outside.yaml"
    sentinel.write_text("keep: me\n")

    with pytest.raises(ValueError):
        t.save_devtype_prompt(bad, "custom", "Custom identifying text.")

    assert sentinel.exists()
    assert "keep: me" in sentinel.read_text()


@pytest.mark.parametrize("bad", _HOSTILE_TYPES)
def test_delete_devtype_prompt_refuses_hostile_dev_type(
        monkeypatch, tmp_path, bad):
    t = _tpl(monkeypatch, tmp_path)
    (tmp_path / "config" / "devtype_prompt_templates").mkdir(parents=True)
    sentinel = tmp_path / "config" / "outside.yaml"
    sentinel.write_text("keep: me\n")

    with pytest.raises(ValueError):
        t.delete_devtype_prompt(bad, "custom")

    assert sentinel.exists()
    assert "keep: me" in sentinel.read_text()


def test_save_devtype_prompt_confined_alone_when_gates_bypassed(
        monkeypatch, tmp_path):
    """With regex / membership gates monkeypatched away, confined alone
    must refuse a hostile dev_type and leave the outside sentinel alone."""
    t = _tpl(monkeypatch, tmp_path)
    root = tmp_path / "config" / "devtype_prompt_templates"
    root.mkdir(parents=True)
    sentinel = tmp_path / "config" / "outside.yaml"
    sentinel.write_text("keep: me\n")

    monkeypatch.setattr(t, "_NAME_RE", re.compile(r".+"))
    # delete_devtype_prompt's charset gate — save has none today; patch the
    # shared authority too so a future gate cannot mask the confined belt.
    if hasattr(t, "_DEV_TYPE_NAME_RE"):
        monkeypatch.setattr(t, "_DEV_TYPE_NAME_RE", re.compile(r".+"))
    import devcake.config as config_mod
    if hasattr(config_mod, "DEV_TYPE_NAME_RE"):
        monkeypatch.setattr(config_mod, "DEV_TYPE_NAME_RE", re.compile(r".+"))

    with pytest.raises(ValueError):
        t.save_devtype_prompt("..", "custom", "Custom identifying text.")

    assert sentinel.exists()
    assert "keep: me" in sentinel.read_text()
    # Nothing written under the templates root from the hostile type.
    assert list(root.iterdir()) == []


def test_save_template_confined_alone_when_type_gate_bypassed(
        monkeypatch, tmp_path):
    """With _require_type / validate_template bypassed, confined refuses
    a hostile mission_type under the constant root."""
    t = _tpl(monkeypatch, tmp_path)
    (tmp_path / "config" / "prompt_templates").mkdir(parents=True)
    sentinel = tmp_path / "config" / "outside.yaml"
    sentinel.write_text("keep: me\n")

    monkeypatch.setattr(t, "_require_type", lambda *_a, **_k: None)
    monkeypatch.setattr(t, "validate_template", lambda *_a, **_k: None)
    monkeypatch.setattr(t, "_NAME_RE", re.compile(r".+"))

    with pytest.raises(ValueError):
        t.save_template("..", "terse", "anything")

    assert sentinel.exists()
    assert "keep: me" in sentinel.read_text()
