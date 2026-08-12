"""completion.py — THE merged-mission chokepoint (ADR-0034; audit F1/F4).

Pins: the three-cause table (labels, byte-exact feed copy, disclosure flag),
run-path checkpoint idempotency, the F4 regression (a PMO failure inside
completion must PROPAGATE, never fall into the merge-failure ladder and post
"auto-merge failed" on an already-merged PR), the AUD-010 trust matrix, and
the source ratchet that keeps the doctrine from forking again."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from devcake.domain.model import MissionRef
from devcake.domain.orchestrator import completion, review
from devcake.domain.orchestrator.markers import COMMENT_SENTINEL
from devcake.domain.run import Run
from devcake.ports.forge import ForgeError, PullRequest

from test_transitions import FakeForge, FakePMO, make_mgr, mission, run_coro


def _review_run():
    return Run(run_id="T-1-1-REVIEW-CCCCCC", mission_key="T-1",
               mission_pmo_id="p1", mission_type="REVIEW",
               dev_type="senior-dev", seq=1,
               stage_label_at_dispatch="DEVCAKE-REVIEW")


CASES = [
    (completion.MergedCause.REVIEW_AUTO_MERGE, "DEVCAKE-REVIEW",
     "✅ REVIEW approved; PR merged (https://forge/pr/8). Mission done.",
     False),
    (completion.MergedCause.SWEEP_EXTERNAL_MERGE, "DEVCAKE-MERGE",
     "✅ PR https://forge/pr/8 merged — mission done (merge sweep).",
     False),
    (completion.MergedCause.DEFERRED_RETRY_MERGE, "DEVCAKE-MERGE",
     "✅ Merged after deferred retry (https://forge/pr/8). Mission done.",
     True),
]


@pytest.mark.parametrize("cause,label,copy,discloses", CASES)
def test_cause_table_labels_copy_and_disclosure(tmp_path, monkeypatch,
                                                cause, label, copy, discloses):
    m = mission("in_progress", {"DEVCAKE", label})
    mgr, fake, store = make_mgr(tmp_path, m)
    pr = PullRequest(number=8, url="https://forge/pr/8", state="open",
                     merged=True)
    disclosed = []

    async def fake_disclose(mgr_, mission_):
        disclosed.append(mission_.pmo_id)

    monkeypatch.setattr(completion.freshness, "disclose_unread_at_close",
                        fake_disclose)
    zipped = []

    async def fake_zip(*a):
        zipped.append(a)

    monkeypatch.setattr(mgr, "deliver_internal_zip", fake_zip)
    monkeypatch.setattr(mgr, "deliver_internal_zip_for_mission", fake_zip)

    run = _review_run() if cause is completion.MergedCause.REVIEW_AUTO_MERGE \
        else None
    if run:
        store.save(run)
    run_coro(completion.complete_merged(
        mgr, cause, ref=MissionRef("p1", "issue"), mission_key="T-1",
        pr=pr, pr_url="https://forge/pr/8",
        run=run, mission=None if run else m))

    assert label not in m.labels, "the stage/park label must come off"
    assert m.status == "done"
    # byte-exact legacy copy + the _feed chokepoint's provenance sentinel
    assert fake.comments == [f"{copy}\n\n{COMMENT_SENTINEL}"]
    assert len(zipped) == 1
    assert disclosed == (["p1"] if discloses else [])
    if run:
        assert "review:done" in run.finalized_steps


def test_run_path_redelivery_skips_core_but_retries_zip(tmp_path, monkeypatch):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    mgr, fake, store = make_mgr(tmp_path, m)
    pr = PullRequest(number=8, url="https://forge/pr/8", state="open")
    zipped = []

    async def fake_zip(*a):
        zipped.append(a)

    monkeypatch.setattr(mgr, "deliver_internal_zip", fake_zip)
    run = _review_run()
    store.save(run)
    for _ in range(2):   # redelivery after a crash between core and ack
        run_coro(completion.complete_merged(
            mgr, completion.MergedCause.REVIEW_AUTO_MERGE,
            ref=MissionRef("p1", "issue"), mission_key="T-1",
            pr=pr, pr_url="https://forge/pr/8", run=run))
    assert len(fake.comments) == 1, "checkpointed core must not re-post"
    assert len(zipped) == 2, "zip has its own idempotency (deliver:zip)"


def test_zip_failure_never_undoes_done(tmp_path, monkeypatch):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-MERGE"})
    mgr, fake, store = make_mgr(tmp_path, m)

    async def boom_zip(*a):
        raise RuntimeError("attachment upload 500")

    monkeypatch.setattr(mgr, "deliver_internal_zip_for_mission", boom_zip)
    run_coro(completion.complete_merged(
        mgr, completion.MergedCause.SWEEP_EXTERNAL_MERGE,
        ref=MissionRef("p1", "issue"), mission_key="T-1",
        pr=SimpleNamespace(url="https://forge/pr/8"),
        pr_url="https://forge/pr/8", mission=m))
    assert m.status == "done", "packaging must never un-Done the mission"


def test_requires_run_or_mission():
    with pytest.raises(AssertionError):
        run_coro(completion.complete_merged(
            None, completion.MergedCause.SWEEP_EXTERNAL_MERGE,
            ref=MissionRef("p1", "issue"), mission_key="T-1",
            pr=None, pr_url="u"))


# ── Sweep restartability: status is the commit point, not the label swap ───


class _BoomSwapPMO(FakePMO):
    async def swap_labels(self, ref, remove, add):
        raise RuntimeError("PMO 502 mid-swap")


class _AlreadyMergedForge(FakeForge):
    async def get_pr_by_branch(self, branch):
        return PullRequest(number=8, url="https://forge/pr/8",
                           state="closed", merged=True)

    async def pr_state(self, pr_number):
        return PullRequest(number=pr_number, url="https://forge/pr/8",
                           state="closed", merged=True)


class _ClosedUnmergedForge(FakeForge):
    async def get_pr_by_branch(self, branch):
        return PullRequest(number=8, url="https://forge/pr/8",
                           state="closed", merged=False)

    async def pr_state(self, pr_number):
        return PullRequest(number=pr_number, url="https://forge/pr/8",
                           state="closed", merged=False)


class _BoomCancelPMO(FakePMO):
    def __init__(self, mission, fail_times=1):
        super().__init__(mission)
        self._cancel_fails = fail_times

    async def cancel_mission(self, ref):
        if self._cancel_fails > 0:
            self._cancel_fails -= 1
            raise RuntimeError("PMO 502 mid-cancel")
        await super().cancel_mission(ref)


def _sweep_mgr(tmp_path, *, pmo, forge):
    from fakes import make_mission_manager
    from devcake.config import AppConfig, DevType
    from test_transitions import NullMessaging
    return make_mission_manager(
        tmp_path, pmo=pmo, forge=forge, config=AppConfig(),
        dev_types={"senior-dev": DevType(name="senior-dev",
                                         harness_template="claude-code")},
        messaging=NullMessaging(), noop_audit=True)


def test_sweep_status_failure_leaves_merge_label_so_retry_works(tmp_path):
    """Status is the derive commit. A failed set_status must leave
    DEVCAKE-MERGE in place so the next sweep still selects the mission.
    Swap-first (the old order) stranded a merged PR at derivation row 9."""
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-MERGE"})
    fake = _FlakyStatusPMO(m, fail_times=1)
    mgr = _sweep_mgr(tmp_path, pmo=fake, forge=FakeForge())
    pr = PullRequest(number=8, url="https://forge/pr/8",
                     state="closed", merged=True)
    with pytest.raises(RuntimeError, match="PMO 502"):
        run_coro(completion.complete_merged(
            mgr, completion.MergedCause.SWEEP_EXTERNAL_MERGE,
            ref=MissionRef("p1", "issue"), mission_key="T-1",
            pr=pr, pr_url="https://forge/pr/8", mission=m))
    assert "DEVCAKE-MERGE" in m.labels
    assert m.status != "done"
    assert fake.comments == []

    run_coro(completion.complete_merged(
        mgr, completion.MergedCause.SWEEP_EXTERNAL_MERGE,
        ref=MissionRef("p1", "issue"), mission_key="T-1",
        pr=pr, pr_url="https://forge/pr/8", mission=m))
    assert m.status == "done"
    assert "DEVCAKE-MERGE" not in m.labels
    assert sum("mission done" in c for c in fake.comments) == 1


def test_status_lands_even_if_label_swap_fails(tmp_path):
    """A Done mission with a leftover MERGE label is row 5 (ignored).
    A swapped-but-not-Done mission is row 9 (stranded). Status first."""
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-MERGE"})
    fake = _BoomSwapPMO(m)
    mgr = _sweep_mgr(tmp_path, pmo=fake, forge=FakeForge())
    with pytest.raises(RuntimeError, match="PMO 502 mid-swap"):
        run_coro(completion.complete_merged(
            mgr, completion.MergedCause.SWEEP_EXTERNAL_MERGE,
            ref=MissionRef("p1", "issue"), mission_key="T-1",
            pr=SimpleNamespace(url="https://forge/pr/8"),
            pr_url="https://forge/pr/8", mission=m))
    assert m.status == "done"
    assert "DEVCAKE-MERGE" in m.labels


def test_sweeps_retries_after_swallowed_status_failure(tmp_path):
    """sweeps() swallows per-mission errors. The next cycle must still
    see an unfinished merged PR (MERGE still on, in_progress) and finish it."""
    from devcake.domain.orchestrator import sweeps
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-MERGE"})
    fake = _FlakyStatusPMO(m, fail_times=1)
    mgr = _sweep_mgr(tmp_path, pmo=fake, forge=_AlreadyMergedForge())
    run_coro(sweeps.sweeps(mgr, [m]))
    assert m.status != "done"
    assert "DEVCAKE-MERGE" in m.labels
    run_coro(sweeps.sweeps(mgr, [m]))
    assert m.status == "done"
    assert "DEVCAKE-MERGE" not in m.labels


def test_closed_unmerged_keeps_merge_label_if_cancel_fails(tmp_path):
    """Same commit-point rule on the cancel twin: strip MERGE only after
    cancel_mission lands, or the next sweep cannot see the mission."""
    from devcake.domain.orchestrator import sweeps
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-MERGE"})
    fake = _BoomCancelPMO(m, fail_times=1)
    mgr = _sweep_mgr(tmp_path, pmo=fake, forge=_ClosedUnmergedForge())
    run_coro(sweeps.sweeps(mgr, [m]))
    assert m.status != "canceled"
    assert "DEVCAKE-MERGE" in m.labels
    run_coro(sweeps.sweeps(mgr, [m]))
    assert m.status == "canceled"
    assert "DEVCAKE-MERGE" not in m.labels


# ── F4: completion failures propagate, never masquerade as merge failures ───


class _MergedOnProbeForge(FakeForge):
    """merge() raises; the re-probe finds the PR already merged."""

    async def pr_state(self, pr_number):
        return PullRequest(number=pr_number, url="https://forge/pr/8",
                           state="closed", merged=True)


class _FlakyStatusPMO(FakePMO):
    def __init__(self, mission, fail_times=1):
        super().__init__(mission)
        self._status_fails = fail_times

    async def set_status(self, ref, status):
        if self._status_fails > 0:
            self._status_fails -= 1
            raise RuntimeError("PMO 502 mid-completion")
        await super().set_status(ref, status)


def test_pmo_failure_inside_completion_propagates_not_merge_failed(tmp_path):
    """2026-08-12 audit F4 regression, pinned red-before: the old except
    scope swallowed a set_status transient inside the found-merged done
    path, logged it as a probe failure, fell into the failure ladder, and
    posted "auto-merge failed" + REVIEW→MERGE on an ALREADY-MERGED PR."""
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = _MergedOnProbeForge(
        merge_exc=ForgeError("PUT merge → 405", status=405))
    from fakes import make_mission_manager
    from devcake.config import AppConfig, DevType
    from test_transitions import NullMessaging
    fake = _FlakyStatusPMO(m)
    mgr = make_mission_manager(
        tmp_path, pmo=fake, forge=forge, config=AppConfig(),
        dev_types={"senior-dev": DevType(name="senior-dev",
                                         harness_template="claude-code")},
        messaging=NullMessaging(), noop_audit=True)
    mgr.forges.instance("main").auto_merge = True
    run = _review_run()
    mgr.runs.store.save(run)

    with pytest.raises(RuntimeError, match="PMO 502"):
        run_coro(review.finalize_review(
            mgr, run, {"verdict": "approve", "report_md": "ok"}))
    assert not any("auto-merge failed" in c for c in fake.comments), (
        "a completion failure must never post the merge-failure copy")
    assert "DEVCAKE-MERGE" not in m.labels
    assert "review:done" not in run.finalized_steps
    # the found-merged stamp was absorbed so redelivery skips the merge
    assert "review:merge" in run.finalized_steps

    # convergence: the redelivery re-enters finalize and completes cleanly
    run_coro(review.finalize_review(
        mgr, run, {"verdict": "approve", "report_md": "ok"}))
    assert m.status == "done"
    assert sum("Mission done." in c for c in fake.comments) == 1
    assert "review:done" in run.finalized_steps


# ── AUD-010 trust matrix ─────────────────────────────────────────────────────


@pytest.mark.parametrize("verdict,tristate,expected", [
    (False, True, True),     # GitHub/GitLab False = real conflict
    (False, False, False),   # Gitea boolean False = maybe not-computed-yet
    (False, None, False),    # missing caps → fail-closed (do not trust)
    (True, True, False),
    (None, True, False),
    (None, False, False),
])
def test_trusted_conflict_matrix(verdict, tristate, expected):
    forge = SimpleNamespace() if tristate is None else SimpleNamespace(
        capabilities=SimpleNamespace(mergeable_tristate=tristate))
    assert completion.trusted_conflict(forge, verdict) is expected


# ── the ratchet: completion doctrine cannot fork again ───────────────────────


def test_completion_doctrine_lives_only_in_completion_py():
    """Source ratchet (ADR-0034): the AUD-010 capability test and the
    completion feed copy exist in exactly one module. A re-inlined copy in
    review.py or sweeps.py — the shape of the original doctrine split —
    turns this red."""
    import devcake.domain.orchestrator as pkg
    root = Path(pkg.__file__).parent
    offenders = []
    for p in sorted(root.glob("*.py")):
        if p.name == "completion.py":
            continue
        src = p.read_text()
        for needle in ("mergeable_tristate", "Mission done.",
                       "merge_sweep_done", "review_approve_merged",
                       "merge_retry_succeeded"):
            if needle in src:
                offenders.append(f"{p.name}: {needle}")
    assert not offenders, (
        f"completion doctrine must live only in completion.py — route these "
        f"through the chokepoint: {offenders}")
