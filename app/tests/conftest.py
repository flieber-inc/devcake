"""Unit tests run with a non-sentinel app digest.

Production images default to DEVCAKE_APP_DIGEST_UNSET (bare bake). Tests
that want the sentinel pass it explicitly to require_staffed / monkeypatch.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _non_sentinel_app_digest(monkeypatch):
    monkeypatch.setenv("DEVCAKE_APP_DIGEST", "sha256:test")
