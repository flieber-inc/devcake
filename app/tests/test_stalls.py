"""stalls.py (ADR-0042 §5 addendum): a mission that cannot start is a
state a person must see — /health, one ✋ notice per block per episode,
the status comment's Now line — and never the Dev's context."""
from datetime import datetime, timedelta, timezone

from devcake.domain.model import ActivityEntry
from devcake.domain.orchestrator import feed, stalls
from devcake.domain.orchestrator.markers import STALL_MARKER, STATUS_MARKER
from devcake.ports.pmo import PMOBudgetExceeded
from test_transitions import make_mgr, mission, run_coro

T0 = datetime(2026, 9, 2, 18, 50, tzinfo=timezone.utc)
UPSTREAM = ("upstream activity unavailable — dispatch deferred: "
            "877ea700: ancestor not in board snapshot")
RECEIPT = "no receipt for grok-build 1.0.25"


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, **kw):
        self.t = self.t + timedelta(**kw)


def _rig(tmp_path, monkeypatch, labels=frozenset({"DEVCAKE", "DEVCAKE-EXECUTE"})):
    m = mission("in_progress", set(labels))
    mgr, fake, store = make_mgr(tmp_path, m)
    fake.record_feed = True
    mgr._stall_path = tmp_path / "stalls.json"
    clock = Clock()
    monkeypatch.setattr(stalls, "utcnow", clock)
    return m, mgr, fake, clock


def _cycle(mgr, m, text):
    """One poll cycle in which the mission's dispatch was refused with `text`
    (None = the mission was not refused this cycle)."""
    stalls.begin_cycle(mgr)
    if text is not None:
        stalls.observe(mgr, m, text)
    run_coro(stalls.end_cycle(mgr, [m]))


def _notices(fake):
    return [c for c in fake.comments if STALL_MARKER in feed.unquoted(c)]


def _statuses(fake):
    return [c for c in fake.comments if STATUS_MARKER in feed.unquoted(c)]


# ── classification ────────────────────────────────────────────────────────

def test_dependency_waits_are_never_stalls():
    assert stalls.classify("blocked by T-2, T-3") is None
    assert stalls.classify("decomposition of T-1 not finalized — the parent issue is still open") is None


def test_loud_kinds_and_subjects():
    assert stalls.classify(RECEIPT) == (stalls.KIND_HARNESS, "grok-build 1.0.25")
    assert stalls.classify(UPSTREAM) == (stalls.KIND_UPSTREAM, "877ea700")
    assert stalls.classify("repo resolve failed at dispatch: X")[0] == stalls.KIND_REPO
    assert stalls.classify("no repository resolved")[0] == stalls.KIND_REPO
    assert stalls.classify("PMO read failed at dispatch: boom")[0] == stalls.KIND_PMO
    assert stalls.classify("EXECUTE is unassigned — set an assignment")[0] == stalls.KIND_CONFIG
    assert stalls.classify("something new")[0] == stalls.KIND_OTHER


# ── threshold and the one-notice rule ────────────────────────────────────

def test_below_threshold_is_silent(tmp_path, monkeypatch):
    m, mgr, fake, clock = _rig(tmp_path, monkeypatch)
    for _ in range(5):
        _cycle(mgr, m, RECEIPT); clock.tick(minutes=2)
    assert fake.comments == [] and stalls.stalled(mgr) == []


def test_past_threshold_one_notice_one_status_then_silence(tmp_path, monkeypatch):
    m, mgr, fake, clock = _rig(tmp_path, monkeypatch)
    _cycle(mgr, m, UPSTREAM)
    clock.tick(minutes=31)
    _cycle(mgr, m, UPSTREAM)
    assert len(_notices(fake)) == 1 and len(_statuses(fake)) == 1
    notice = _notices(fake)[0]
    assert "cannot start" in notice and "877ea700" in notice
    assert "Unarchive it" in notice
    status = _statuses(fake)[0]
    assert "Waiting to start since 2026-09-02 18:50 UTC" in status
    edits_before = len(getattr(fake, "edits", []))
    for _ in range(100):                       # a week of the same stall
        clock.tick(hours=1); _cycle(mgr, m, UPSTREAM)
    assert len(_notices(fake)) == 1 and len(_statuses(fake)) == 1
    assert len(getattr(fake, "edits", [])) == edits_before
    row, = stalls.stalled(mgr)
    assert row["kind"] == "upstream" and row["severity"] == "critical"
    assert row["since"] == T0.isoformat()


def test_a_different_block_notifies_again(tmp_path, monkeypatch):
    m, mgr, fake, clock = _rig(tmp_path, monkeypatch)
    _cycle(mgr, m, UPSTREAM); clock.tick(minutes=31); _cycle(mgr, m, UPSTREAM)
    assert len(_notices(fake)) == 1
    clock.tick(hours=2)
    _cycle(mgr, m, RECEIPT)                    # new identity: new clock
    assert len(_notices(fake)) == 1            # below threshold again
    clock.tick(minutes=31); _cycle(mgr, m, RECEIPT)
    assert len(_notices(fake)) == 2
    assert "grok-build 1.0.25" in _notices(fake)[1]
    assert len(_statuses(fake)) == 1           # the view is edited, not reposted
    assert any("grok-build" in body for _p, _e, body in fake.edits)


def test_flicker_within_an_episode_is_silent(tmp_path, monkeypatch):
    m, mgr, fake, clock = _rig(tmp_path, monkeypatch)
    _cycle(mgr, m, UPSTREAM); clock.tick(minutes=31); _cycle(mgr, m, UPSTREAM)
    clock.tick(hours=2)
    _cycle(mgr, m, None)                       # the block vanished for a cycle
    assert stalls.stalled(mgr) == []           # cleared: not on the panel
    clock.tick(minutes=5)
    _cycle(mgr, m, UPSTREAM)                   # and came back
    clock.tick(hours=2); _cycle(mgr, m, UPSTREAM)
    assert len(_notices(fake)) == 1            # same occurrence: no news
    assert len(stalls.stalled(mgr)) == 1


def test_return_after_a_day_without_dispatch_is_new(tmp_path, monkeypatch):
    m, mgr, fake, clock = _rig(tmp_path, monkeypatch)
    _cycle(mgr, m, UPSTREAM); clock.tick(minutes=31); _cycle(mgr, m, UPSTREAM)
    clock.tick(hours=2); _cycle(mgr, m, None)
    clock.tick(days=1, minutes=1); _cycle(mgr, m, UPSTREAM)
    clock.tick(minutes=31); _cycle(mgr, m, UPSTREAM)
    assert len(_notices(fake)) == 2


def test_dispatch_ends_the_episode(tmp_path, monkeypatch):
    m, mgr, fake, clock = _rig(tmp_path, monkeypatch)
    _cycle(mgr, m, UPSTREAM); clock.tick(minutes=31); _cycle(mgr, m, UPSTREAM)
    stalls.dispatched(mgr, m.pmo_id)           # AIDEV-219 unarchived, it ran
    assert stalls.ledger(mgr).stalls == {}
    clock.tick(hours=3)
    _cycle(mgr, m, UPSTREAM)                   # archived again
    clock.tick(minutes=31); _cycle(mgr, m, UPSTREAM)
    assert len(_notices(fake)) == 2            # a new episode: news again


def test_a_person_parking_it_ends_the_episode(tmp_path, monkeypatch):
    m, mgr, fake, clock = _rig(tmp_path, monkeypatch)
    _cycle(mgr, m, UPSTREAM)
    m.labels.add("DEVCAKE-NEEDS-HUMAN")
    _cycle(mgr, m, None)
    assert stalls.ledger(mgr).stalls == {}


def test_write_cap_bounds_a_flapping_identity(tmp_path, monkeypatch):
    m, mgr, fake, clock = _rig(tmp_path, monkeypatch)
    _cycle(mgr, m, UPSTREAM); clock.tick(minutes=31); _cycle(mgr, m, UPSTREAM)
    for i in range(20):                        # alternating blocks, minutes apart
        clock.tick(minutes=2)
        _cycle(mgr, m, RECEIPT if i % 2 else UPSTREAM)
    assert len(fake.comments) + len(getattr(fake, "edits", [])) <= 4


def test_budget_refusal_is_retried_next_cycle(tmp_path, monkeypatch):
    m, mgr, fake, clock = _rig(tmp_path, monkeypatch)
    calls = {"n": 0}
    real = mgr._feed

    async def refusing(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PMOBudgetExceeded("spent")
        return await real(*a, **k)
    mgr._feed = refusing
    _cycle(mgr, m, UPSTREAM); clock.tick(minutes=31); _cycle(mgr, m, UPSTREAM)
    assert _notices(fake) == []                # refused, nothing recorded
    clock.tick(minutes=1); _cycle(mgr, m, UPSTREAM)
    assert len(_notices(fake)) == 1            # written on the next cycle


def test_ledger_survives_a_restart(tmp_path, monkeypatch):
    m, mgr, fake, clock = _rig(tmp_path, monkeypatch)
    _cycle(mgr, m, UPSTREAM)
    fresh = stalls.StallLedger(mgr.instance_name, tmp_path / "stalls.json")
    assert fresh.stalls[m.pmo_id].first_seen == T0.isoformat()


def test_project_kind_is_clocked_but_never_written(tmp_path, monkeypatch):
    m, mgr, fake, clock = _rig(tmp_path, monkeypatch)
    m.pmo_kind = "project"
    _cycle(mgr, m, RECEIPT); clock.tick(minutes=31); _cycle(mgr, m, RECEIPT)
    assert fake.comments == [] and len(stalls.stalled(mgr)) == 1


# ── never the Dev's context ───────────────────────────────────────────────

def test_stall_notice_and_status_are_dropped_from_the_dev_folder(tmp_path, monkeypatch):
    m, mgr, fake, clock = _rig(tmp_path, monkeypatch)
    _cycle(mgr, m, UPSTREAM); clock.tick(minutes=31); _cycle(mgr, m, UPSTREAM)
    entries = [ActivityEntry(ts=T0 + timedelta(minutes=i), author="devcake",
                             kind="comment", body=c, entry_id=f"c{i}")
               for i, c in enumerate(fake.comments)]
    human = ActivityEntry(ts=T0, author="felipe", kind="comment",
                          body="please hurry", entry_id="h1")
    projected = feed.unfold_entries([human, *entries])
    assert [p.body for p in projected] == ["please hurry"]


# ── the scheduler is the one observer ────────────────────────────────────

def test_scheduler_clocks_refusals_and_ignores_gates(tmp_path, monkeypatch):
    from fakes import FakeForgeRuntime, make_mission_manager
    from devcake.adapters.files.run_store import RunStore
    from devcake.config import AppConfig, DevType, PMOInstance
    from test_repo_routing import _m, REASON_ZERO_REPO
    mgr = make_mission_manager(
        tmp_path, pmo=None, forge_runtime=FakeForgeRuntime(None),
        config=AppConfig(),
        instance=PMOInstance(name="linear", team_key="DEV", repos=["main"]),
        dev_types={"senior-dev": DevType(name="senior-dev",
                                         harness_template="claude-code")},
        runs=type("Runs", (), {"store": RunStore(tmp_path / "runs")})(),
    )
    mgr._stall_path = tmp_path / "stalls.json"
    m = _m(); m.labels = {"DEVCAKE"}
    m.repo, m.repo_reason = None, REASON_ZERO_REPO
    run_coro(mgr.schedule([m], gate={}))
    assert stalls.ledger(mgr).stalls[m.pmo_id].kind == stalls.KIND_REPO
    gated = _m(); gated.pmo_id = "p2"; gated.key = "T-2"; gated.labels = {"DEVCAKE"}
    run_coro(mgr.schedule([gated], gate={"p2": "blocked by T-1"}))
    assert "p2" not in stalls.ledger(mgr).stalls
