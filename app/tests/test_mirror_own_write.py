"""Own-write mirror invalidation (ADR-0024 addendum item 7): a finished
run, a mission completing on a merged pull request, a claims push, or a
Clear pruning the claims drops that repository's mirror freshness so the
next dispatch resyncs regardless of `sync_max_age_seconds`.

Every test drives a public seam with a recording cache — no source-text
assertions:
- MissionManager.finalize (success, failure, no repository)
- review.finalize_review: the finalize-time auto-merge, a merge that
  raised but landed (found merged on the re-probe), a real conflict
- sweeps.merge_sweep: the deferred retry, a failed retry, an external merge
- the claims conveyor at finalize (written notebooks only)
- clear_all: pruned notebooks only
The mid-sync race itself is covered in test_repo_mirror.py.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from devcake.domain.repo_mirror import NullRepoCache
from devcake.ports.forge import PullRequest


class RecordingCache(NullRepoCache):
    def __init__(self):
        super().__init__()
        self.invalidated: list[str] = []

    def invalidate(self, name):
        self.invalidated.append(name)


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


# ── the finalize verb ────────────────────────────────────────────────────────

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
    # no repository reference → nothing to invalidate
    run.repo_ref = ""
    monkeypatch.setattr(finalize_mod, "finalize", ok)
    run_coro(mgr.finalize(run, {}))
    assert mgr.repo_cache.invalidated == ["work", "work"]


# ── finalize-time merges (review.py → completion chokepoint) ────────────────

def test_finalize_auto_merge_invalidates_through_the_chokepoint(tmp_path):
    from test_freshness_gate import SENTINEL, _entry, _gate_mgr, _review_run
    from devcake.domain.orchestrator import review
    m, mgr, fake = _gate_mgr(tmp_path, [_entry("e1", "brief" + SENTINEL,
                                               author="devcake")])
    inst = mgr.forges.instance("main")
    inst.merge_settle_minutes = 0
    mgr.repo_cache = RecordingCache()
    run = _review_run(watermark_id="e1")
    run.repo_ref = "main"
    run_coro(review.finalize_review(
        mgr, run, {"verdict": "approve", "report_md": "ok"}))
    assert mgr.forges.get("main").merges == [8]
    assert "done" in fake.statuses
    assert mgr.repo_cache.invalidated == ["main"]


def test_merge_that_raised_but_landed_still_invalidates(tmp_path):
    """The app's merge call timed out AFTER landing: forge.merge raises,
    the re-probe finds the PR merged, completion runs — the repository
    changed, the mirror must know."""
    from test_transitions import _approve_review, merge_fail_mgr
    m, mgr, fake, forge = merge_fail_mgr(tmp_path, mergeable_result=True)

    async def merged(pr_number):
        return PullRequest(number=pr_number, url="https://forge/pr/8",
                           state="closed", merged=True)
    forge.pr_state = merged
    mgr.repo_cache = RecordingCache()
    run_coro(_approve_review(mgr))
    assert "done" in fake.statuses
    assert mgr.repo_cache.invalidated == ["main"]


def test_real_merge_conflict_invalidates_nothing(tmp_path):
    from test_transitions import _approve_review, merge_fail_mgr
    m, mgr, fake, forge = merge_fail_mgr(tmp_path, mergeable_result=False)
    mgr.repo_cache = RecordingCache()
    run_coro(_approve_review(mgr))
    assert "DEVCAKE-EXECUTE" in m.labels                # routed to rework
    assert mgr.repo_cache.invalidated == []


# ── the merge sweep ──────────────────────────────────────────────────────────

def test_sweep_retry_merge_invalidates_and_a_failed_retry_does_not(tmp_path):
    from test_transitions import sweep_mgr
    from devcake.domain.orchestrator import sweeps
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    mgr.repo_cache = RecordingCache()
    run_coro(sweeps.merge_sweep(mgr, m))
    assert forge.merges == [8] and m.status == "done"
    assert mgr.repo_cache.invalidated == ["main"]

    m2, mgr2, fake2, forge2 = sweep_mgr(tmp_path / "b", mergeable_result=True,
                                        merge_exc=RuntimeError("502"))
    mgr2.repo_cache = RecordingCache()
    run_coro(sweeps.merge_sweep(mgr2, m2))
    assert forge2.merges == [8] and m2.status != "done"
    assert mgr2.repo_cache.invalidated == []


def test_external_merge_found_by_the_sweep_invalidates(tmp_path):
    """auto_merge off is the default: a human merges, the sweep finds the
    PR merged and completes the mission. DevCake did not write, but it
    now KNOWS the repository changed — dependent children dispatch on the
    very next cycle and must resync."""
    from test_transitions import sweep_mgr
    from devcake.domain.orchestrator import sweeps
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=None)
    mgr.forges.instance("main").auto_merge = False

    async def merged(pr_number):
        return PullRequest(number=pr_number, url="https://forge/pr/8",
                           state="closed", merged=True)
    forge.pr_state = merged
    mgr.repo_cache = RecordingCache()
    run_coro(sweeps.merge_sweep(mgr, m))
    assert forge.merges == [] and m.status == "done"
    assert mgr.repo_cache.invalidated == ["main"]


# ── the claims conveyor ──────────────────────────────────────────────────────

def test_claims_conveyor_invalidates_only_the_notebooks_it_wrote(tmp_path):
    from test_claims import FakeNotebooks
    from test_discovery_harvest import ENTRY, _exec_run, _harvest_mgr, _payload
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    mgr.claims = FakeNotebooks(writable={"notes"})
    mgr.repo_cache = RecordingCache()
    run = _exec_run(store)
    run.repo_ref = "main"
    run.memory_mounts = [{"card": "notes"}, {"card": "ro"}]
    run_coro(mgr.finalize(run, _payload([ENTRY])))
    assert any(c[0] == "notes" for c in mgr.claims.commits)
    inv = mgr.repo_cache.invalidated
    assert "notes" in inv and "ro" not in inv
    assert inv[-1] == "main"            # the finalize belt, last


# ── clear-runs ───────────────────────────────────────────────────────────────

def test_clear_invalidates_the_notebooks_it_pruned(monkeypatch):
    import devcake.api.clear as clear_mod
    from test_clear import _stub_clear_subsystems
    _stub_clear_subsystems(monkeypatch)

    async def prune(claims, cards):
        return {"nb1": 2, "nb2": 0}
    monkeypatch.setattr("devcake.domain.claims.prune_all", prune)
    monkeypatch.setattr("devcake.config.memory_bound_names",
                        lambda cfg: ["nb1", "nb2"])
    cache = RecordingCache()
    out = run_coro(clear_mod.clear_all(
        None, None, None, claims=SimpleNamespace(), config=SimpleNamespace(),
        repo_cache=cache))
    assert out["claims_pruned"] == {"nb1": 2, "nb2": 0}
    assert cache.invalidated == ["nb1"]

    async def failed(claims, cards):
        return {"error": "notebook unreachable"}
    monkeypatch.setattr("devcake.domain.claims.prune_all", failed)
    cache2 = RecordingCache()
    run_coro(clear_mod.clear_all(
        None, None, None, claims=SimpleNamespace(), config=SimpleNamespace(),
        repo_cache=cache2))
    assert cache2.invalidated == []


def test_null_cache_accepts_invalidate():
    NullRepoCache().invalidate("anything")
