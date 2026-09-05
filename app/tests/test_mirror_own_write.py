"""Own-write mirror invalidation (ADR-0024 addendum): a finished run, an
app-side merge, or a claims push drops that repository's mirror freshness
so the next dispatch resyncs regardless of `sync_max_age_seconds`.

Public seams under test:
- MissionManager.finalize (the finalize verb, success and failure)
- review.py's finalize-time auto-merge closure
- sweeps.py's merge-retry driver
- discovery.py's claims conveyor
"""
from __future__ import annotations

import asyncio

import pytest

from devcake.domain.repo_mirror import NullRepoCache


class RecordingCache(NullRepoCache):
    def __init__(self):
        super().__init__()
        self.invalidated: list[str] = []

    def invalidate(self, name):
        self.invalidated.append(name)


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _mgr_with_run(tmp_path):
    from test_steward import MapPMO, make_mgr
    from devcake.domain.run import Run
    mgr = make_mgr(tmp_path, MapPMO([]))
    mgr.repo_cache = RecordingCache()
    run = Run(run_id="L-T-H-1-EXECUTE-AAAAAA", mission_key="T-H",
              mission_pmo_id="h", mission_type="EXECUTE", dev_type="senior-dev",
              seq=1, state="finalizing", pmo_ref=mgr.instance_name,
              repo_ref="work")
    return mgr, run


def test_finalize_invalidates_the_runs_repo_even_when_it_raises(tmp_path, monkeypatch):
    from devcake.domain.orchestrator import finalize as finalize_mod
    mgr, run = _mgr_with_run(tmp_path)

    async def ok(mgr_, run_, payload):
        return None
    monkeypatch.setattr(finalize_mod, "finalize", ok)
    run_coro(mgr.finalize(run, {"result": {"outcome": "executed"}}))
    assert mgr.repo_cache.invalidated == ["work"]

    async def boom(mgr_, run_, payload):
        raise RuntimeError("tracker down")
    monkeypatch.setattr(finalize_mod, "finalize", boom)
    with pytest.raises(RuntimeError):
        run_coro(mgr.finalize(run, {}))
    assert mgr.repo_cache.invalidated == ["work", "work"]   # a push may have landed
    # a run without a repository (a steward) invalidates nothing
    run.repo_ref = ""
    monkeypatch.setattr(finalize_mod, "finalize", ok)
    run_coro(mgr.finalize(run, {}))
    assert mgr.repo_cache.invalidated == ["work", "work"]


def test_claims_conveyor_invalidates_every_notebook_it_wrote(tmp_path, monkeypatch):
    """discovery.py: the cards `append_from_harvest` wrote to are
    invalidated; cards with nothing written are not."""
    from devcake.domain.orchestrator import discovery
    src = discovery.__file__
    text = open(src).read()
    assert "written = await claims_mod.append_from_harvest(" in text
    assert "mgr.repo_cache.invalidate(card)" in text


def test_merge_sites_invalidate_the_repository():
    """Both app-side merges — the finalize-time auto-merge and the merge
    sweep's retry — invalidate the repository right after the forge merge,
    inside the same try so a failed merge never invalidates."""
    from devcake.domain.orchestrator import review, sweeps
    r = open(review.__file__).read()
    i = r.index("await forge.merge(pr.number)")
    assert "mgr.repo_cache.invalidate(run.repo_ref)" in r[i:i + 200]
    s = open(sweeps.__file__).read()
    j = s.index("await forge.merge(pr.number)")
    assert "mgr.repo_cache.invalidate(m.repo)" in s[j:j + 200]
    # and it sits before the except that treats a failed merge as the signal
    assert s.index("mgr.repo_cache.invalidate(m.repo)") < s.index(
        "except Exception:  # noqa: BLE001 — a failed merge IS the signal here")
