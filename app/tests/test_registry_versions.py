"""RegistryVersionSource — empty/whitespace stable feed stays in ValueError."""

from __future__ import annotations

import pytest

from devcake.adapters.registry_versions import GROK_STABLE, RegistryVersionSource


def test_empty_stable_feed_raises_value_error(monkeypatch):
    monkeypatch.setattr(
        "devcake.adapters.registry_versions._http_text",
        lambda url: "",
    )
    with pytest.raises(ValueError, match="empty") as excinfo:
        RegistryVersionSource().latest("grok-build")
    assert GROK_STABLE in str(excinfo.value)
    assert not isinstance(excinfo.value, IndexError)


def test_whitespace_only_stable_feed_raises_value_error(monkeypatch):
    monkeypatch.setattr(
        "devcake.adapters.registry_versions._http_text",
        lambda url: "   \n\n",
    )
    with pytest.raises(ValueError, match="empty") as excinfo:
        RegistryVersionSource().latest("grok-build")
    assert GROK_STABLE in str(excinfo.value)
