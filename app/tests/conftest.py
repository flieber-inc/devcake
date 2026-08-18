"""Unit tests run with a non-sentinel app digest.

Production images default to DEVCAKE_APP_DIGEST_UNSET (bare bake). Tests
that want the sentinel pass it explicitly to require_staffed / monkeypatch.

Staffing is fail-closed on baker liveness. Integration tests that are not
about the baker get baker_alive=True by default via a require_staffed wrap;
death tests call REAL_REQUIRE_STAFFED or pass baker_alive=False.
"""

from __future__ import annotations

import pytest

from devcake import staffing as _staffing_mod

# Unwrapped production seam — death tests call this directly.
REAL_REQUIRE_STAFFED = _staffing_mod.require_staffed


@pytest.fixture(autouse=True)
def _non_sentinel_app_digest(monkeypatch):
    monkeypatch.setenv("DEVCAKE_APP_DIGEST", "sha256:test")


@pytest.fixture(autouse=True)
def _default_live_baker_for_staffing(monkeypatch):
    """Dispatch/OAuth/steward integration paths omit baker_alive; without a
    host baker heartbeat they would all refuse. Default None → True here.
    baker_liveness itself is not stubbed (bake_status / health tests need it).
    """

    def _wrapped(dev_type, *, digest=None, store, baker_alive=None):
        if baker_alive is None:
            baker_alive = True
        return REAL_REQUIRE_STAFFED(
            dev_type, digest=digest, store=store, baker_alive=baker_alive)

    monkeypatch.setattr(_staffing_mod, "require_staffed", _wrapped)
    monkeypatch.setattr(
        "devcake.domain.orchestrator.dispatch.require_staffed", _wrapped)
    monkeypatch.setattr(
        "devcake.domain.orchestrator.steward.require_staffed", _wrapped)
    monkeypatch.setattr(
        "devcake.domain.oauth.require_staffed", _wrapped)
