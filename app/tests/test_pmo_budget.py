"""The PMO request budget (ADR-0040): one vendor-neutral governor in front of
every PMO adapter's wire call. Fake clock throughout — nothing sleeps."""

import asyncio
import re
from pathlib import Path

import httpx
import pytest

from devcake.adapters import budget as B
from devcake.adapters.registry import PMO_SYSTEMS
from devcake.ports import pmo as pmo_port
from devcake.ports.pmo import PMOBudgetExceeded, PMOTransient, pmo_call

HOST = "tracker.example"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.mono = 500.0
        self.sleeps: list[float] = []

    def time(self):
        return self.t

    def monotonic(self):
        return self.mono

    async def sleep(self, s):
        self.sleeps.append(s)
        self.t += s
        self.mono += s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(B, "_clock", c.time)
    monkeypatch.setattr(B, "_mono", c.monotonic)
    monkeypatch.setattr(pmo_port, "_mono", c.monotonic)
    monkeypatch.setattr(B, "_sleep", c.sleep)
    monkeypatch.setattr(B, "_OFF", False)
    return c


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def resp(status=200):
    return httpx.Response(status, json={})


def sender(*items):
    """A `send` returning the queued responses (or raising queued errors)."""
    queue = list(items)
    calls: list[int] = []

    async def send():
        calls.append(1)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    return send, calls


def reader(*signals):
    """A `read` returning the queued signals, then RateSignal()."""
    queue = list(signals)

    def read(_resp):
        return queue.pop(0) if queue else B.RateSignal()
    return read


def bucket(clock, **observed):
    b = B.budget_for(HOST, "key-1", system="tracker", instance="a")
    if observed:
        b.observe(B.RateSignal(**observed), now=clock.t)
    return b


def full_window(clock, limit, remaining, **extra):
    """A fixed-window vendor (GitHub-shaped) with a full hour to the reset."""
    return dict(limit=limit, remaining=remaining, reset_at=clock.t + 3600,
                window_s=3600, **extra)


def leaky(clock, limit, remaining, **extra):
    """A continuously refilling bucket (Linear-shaped)."""
    return dict(limit=limit, remaining=remaining, reset_at=clock.t + 3600,
                window_s=3600, refill="continuous", **extra)


# ── pass-through and observation ─────────────────────────────────────────────

def test_unknown_limit_passes_through(clock):
    b = bucket(clock)
    send, calls = sender(resp())
    out = run(b.request(send, reader(), instance="a"))
    assert out.status_code == 200 and calls == [1]
    assert b.served == {"critical": 0, "routine": 1}


def test_headers_are_authoritative_and_reset_local_spend(clock):
    b = bucket(clock)
    send, _ = sender(resp(), resp(), resp())
    run(b.request(send, reader(), instance="a"))
    run(b.request(send, reader(), instance="a"))
    assert b._local_spent == 2
    run(b.request(send, reader(B.RateSignal(limit=100, remaining=50,
                                             reset_at=clock.t + 3600)),
                  instance="a"))
    assert (b.limit, b.remaining, b._local_spent) == (100, 50, 0)


# ── routine class: paced, refused, never sleeps ──────────────────────────────

def test_routine_refused_at_the_reserve_without_a_vendor_call(clock):
    b = bucket(clock, **full_window(clock, 100, 15))
    send, calls = sender(resp())
    with pytest.raises(PMOBudgetExceeded) as ei:
        run(b.request(send, reader(), instance="a"))
    assert calls == [] and clock.sleeps == []
    assert ei.value.retry_after and ei.value.retry_after > 0
    assert "reserved" in str(ei.value)
    assert b.rejections["routine"] == 1


def test_routine_ahead_of_the_pace_line_is_refused(clock):
    # a full hour to the reset with 80 % left: spent faster than uniform
    b = bucket(clock, **full_window(clock, 1000, 800))
    send, calls = sender(resp())
    with pytest.raises(PMOBudgetExceeded) as ei:
        run(b.request(send, reader(), instance="a"))
    assert calls == [] and clock.sleeps == []
    assert "pace line" in str(ei.value)
    # exact formula: deficit (900 − 800) tokens at limit/window tokens per s
    assert ei.value.retry_after == pytest.approx(100 * 3600 / 1000)


def test_routine_within_the_burst_allowance_goes(clock):
    b = bucket(clock, **full_window(clock, 1000, 950))
    send, calls = sender(resp())
    run(b.request(send, reader(), instance="a"))
    assert calls == [1]


def test_routine_vendor_rejection_raises_without_retry_and_blocks(clock):
    b = bucket(clock, **full_window(clock, 1000, 990))
    send, calls = sender(resp(429), resp())
    limited = B.RateSignal(limited=True, retry_after_s=7)
    with pytest.raises(PMOTransient) as ei:
        run(b.request(send, reader(limited), instance="a"))
    assert not isinstance(ei.value, PMOBudgetExceeded)
    assert calls == [1] and ei.value.retry_after == pytest.approx(7)
    # the block holds for the next routine call — refused, no send
    with pytest.raises(PMOBudgetExceeded):
        run(b.request(send, reader(), instance="a"))
    assert calls == [1] and b.limited_seen == 1


# ── critical class: reserve, waits, one retry ────────────────────────────────

def test_critical_spends_the_reserve_without_waiting(clock):
    b = bucket(clock, **full_window(clock, 100, 10))
    send, calls = sender(resp())
    with pmo_call("critical"):
        run(b.request(send, reader(), instance="a"))
    assert calls == [1] and clock.sleeps == []


def test_critical_waits_for_the_refill_when_exhausted(clock):
    b = bucket(clock, **leaky(clock, 100, 0))
    send, calls = sender(resp())
    with pmo_call("critical"):
        run(b.request(send, reader(), instance="a"))
    assert calls == [1]
    assert clock.sleeps == [pytest.approx(36.0)]     # one token = window/limit
    assert b.waits == 1 and b.wait_seconds == pytest.approx(36.0)


def test_critical_deadline_refuses_instead_of_overwaiting(clock):
    b = bucket(clock, **leaky(clock, 100, 0))
    send, calls = sender(resp())
    with pmo_call("critical", wait_budget_s=10):
        with pytest.raises(PMOBudgetExceeded) as ei:
            run(b.request(send, reader(), instance="a"))
    assert calls == [] and clock.sleeps == []
    assert ei.value.retry_after == pytest.approx(36.0)
    assert b.rejections["critical"] == 1


def test_critical_retries_once_after_a_definitive_rejection(clock):
    b = bucket(clock, **full_window(clock, 1000, 990))
    send, calls = sender(resp(429), resp())
    with pmo_call("critical"):
        out = run(b.request(send, reader(B.RateSignal(limited=True, retry_after_s=2)),
                            instance="a"))
    assert out.status_code == 200 and calls == [1, 1]
    assert clock.sleeps == [pytest.approx(2.0)]


def test_critical_second_rejection_is_the_vendors_error(clock):
    b = bucket(clock, **full_window(clock, 1000, 990))
    send, calls = sender(resp(429), resp(429))
    limited = B.RateSignal(limited=True, retry_after_s=1)
    with pmo_call("critical"):
        with pytest.raises(PMOTransient) as ei:
            run(b.request(send, reader(limited, limited), instance="a"))
    assert not isinstance(ei.value, PMOBudgetExceeded) and calls == [1, 1]


def test_network_errors_are_never_retried_by_the_governor(clock):
    b = bucket(clock)
    send, calls = sender(httpx.ConnectError("boom"), resp())
    with pmo_call("critical"):
        with pytest.raises(httpx.ConnectError):
            run(b.request(send, reader(), instance="a"))
    assert calls == [1]


def test_wait_budget_is_cumulative_per_call_context(clock):
    b = bucket(clock, **leaky(clock, 100, 0))
    exhausted = B.RateSignal(limit=100, remaining=0, reset_at=clock.t + 3600,
                             window_s=3600, refill="continuous")
    send, calls = sender(resp(), resp())
    with pmo_call("critical", wait_budget_s=50):
        run(b.request(send, reader(exhausted), instance="a"))     # waits 36 s
        with pytest.raises(PMOBudgetExceeded):                    # 36 more > 50
            run(b.request(send, reader(), instance="a"))
    assert calls == [1] and clock.sleeps == [pytest.approx(36.0)]


# ── the local model between observations ─────────────────────────────────────

def test_estimate_refills_between_observations(clock):
    b = bucket(clock, **leaky(clock, 3600, 0))
    clock.t += 100
    assert b._remaining_est(clock.t) == pytest.approx(100.0)
    send, calls = sender(resp())
    with pmo_call("critical"):
        run(b.request(send, reader(), instance="a"))   # ≥ 1 token: no wait
    assert calls == [1] and clock.sleeps == []


def test_complexity_fraction_tightens_the_estimate(clock):
    b = bucket(clock, **full_window(clock, 1000, 900, complexity_fraction=0.05))
    send, calls = sender(resp())
    with pytest.raises(PMOBudgetExceeded):
        run(b.request(send, reader(), instance="a"))
    assert calls == []


def test_stale_reset_is_treated_as_unknown(clock):
    # a reset two windows in the past (clock skew / dead header) must not
    # trip the pace line: only the reserve applies
    b = bucket(clock, limit=1000, remaining=800, reset_at=clock.t - 7200,
               window_s=3600)
    send, calls = sender(resp())
    run(b.request(send, reader(), instance="a"))
    assert calls == [1]


def test_foreign_spend_is_noticed(clock):
    b = bucket(clock, **full_window(clock, 1000, 900))
    send, _ = sender(resp())
    # we made one call; the vendor says 50 fewer than that explains
    run(b.request(send, reader(B.RateSignal(limit=1000, remaining=849,
                                             reset_at=clock.t + 3600)),
                  instance="a"))
    assert b.foreign_spend == 50


# ── identity: credential on host, merged by principal ────────────────────────

def test_same_credential_shares_one_bucket_across_systems_and_rebuilds():
    a = B.budget_for(HOST, "k", system="alpha_issues", instance="one")
    b = B.budget_for(HOST, "k", system="beta_issues", instance="two")
    assert a is b and a.systems == {"alpha_issues", "beta_issues"}
    assert a.instances == {"one", "two"}
    assert B.budget_for("other.example", "k", system="alpha_issues") is not a


def test_bind_principal_merges_two_keys_of_one_user(clock):
    b1 = B.budget_for(HOST, "k1", system="tracker", instance="one")
    b2 = B.budget_for(HOST, "k2", system="tracker", instance="two")
    b1.observe(B.RateSignal(limit=100, remaining=40), now=clock.t)
    b2.observe(B.RateSignal(limit=100, remaining=30), now=clock.t + 1)
    assert B.bind_principal(b1, "user-7") is b1
    merged = B.bind_principal(b2, "user-7")
    assert merged is b1 and b1.principal == "user-7"
    assert b1.remaining == 30                     # newest observation wins
    assert b1.instances == {"one", "two"}
    # a rebuilt adapter on either key finds the merged bucket
    assert B.budget_for(HOST, "k2", system="tracker") is b1
    assert B.budget_for(HOST, "k1", system="tracker") is b1
    assert list(B.snapshot_all()) == [b1.label]


def test_snapshot_reports_demand_after_five_minutes(clock):
    b = bucket(clock)
    send, _ = sender(*[resp() for _ in range(12)])
    for _ in range(12):
        run(b.request(send, reader(), instance="a"))
        clock.t += 30                                  # 12 calls over 6 min
    snap = b.snapshot(now=clock.t)
    assert snap["demand_per_hour"]["a"] == pytest.approx(12 * 3600 / 360, abs=2)
    assert snap["label"] == f"{HOST}/key-{b.fingerprint[:6]}"
    assert snap["served"]["routine"] == 12 and snap["limit"] is None


def test_off_switch_observes_but_never_waits_or_refuses(clock, monkeypatch):
    monkeypatch.setattr(B, "_OFF", True)
    b = bucket(clock, **full_window(clock, 100, 0))
    send, calls = sender(resp(), resp(429))
    run(b.request(send, reader(), instance="a"))          # no refusal
    with pmo_call("critical"):
        with pytest.raises(PMOTransient):                 # no retry either
            run(b.request(send, reader(B.RateSignal(limited=True)),
                          instance="a"))
    assert calls == [1, 1] and clock.sleeps == []


# ── structure ratchets ──────────────────────────────────────────────────────

ADAPTERS = Path(__file__).resolve().parents[1] / "devcake" / "adapters"


def test_every_pmo_adapter_routes_its_wire_call_through_the_budget():
    """ADR-0034 chokepoint discipline applied to quota: each PMO adapter's
    HTTP send goes through `_budget.request` with its own signal mapper."""
    for vid in sorted(PMO_SYSTEMS):
        src = (ADAPTERS / vid / "adapter.py").read_text()
        assert "_budget.request(" in src, vid
        assert re.search(r"^def rate_signal\(", src, re.M), vid
        assert "budget_for(" in src, vid


def test_the_governor_names_no_vendor():
    """Code, not prose: identifiers and imports of budget.py carry no vendor
    name (a docstring may explain Linear's per-user rule)."""
    import io
    import tokenize
    src = (ADAPTERS / "budget.py").read_text()
    code = " ".join(
        tok.string for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING)).lower()
    for vid in PMO_SYSTEMS:
        assert vid.split("_")[0] not in code, vid


# ── review round: wait budget, foreign spend, merge, reload ──────────────────

def test_wait_budget_counts_waiting_only_not_other_work(clock):
    """A finalize that spends minutes on git before its first PMO call has
    its whole wait budget left: the budget bounds time spent WAITING."""
    b = bucket(clock, **leaky(clock, 100, 0))
    send, calls = sender(resp())
    with pmo_call("critical", wait_budget_s=50) as ctx:
        clock.mono += 600                       # ten minutes of forge work
        clock.t += 600
        b.observe(B.RateSignal(limit=100, remaining=0, reset_at=clock.t + 3600,
                               window_s=3600, refill="continuous"), now=clock.t)
        run(b.request(send, reader(), instance="a"))   # waits 36 s, allowed
        assert ctx.waited_s == pytest.approx(36.0)
    assert calls == [1] and clock.sleeps == [pytest.approx(36.0)]


def test_foreign_spend_ignores_the_refill_on_a_fixed_window(clock):
    # 30 s later the vendor reports exactly our own spend: no foreign spend
    b = bucket(clock, limit=2000, remaining=1500, reset_at=clock.t + 60,
               window_s=60)
    send, _ = sender(resp())
    clock.t += 30
    run(b.request(send, reader(B.RateSignal(limit=2000, remaining=1499,
                                             reset_at=clock.t + 30)),
                  instance="a"))
    assert b.foreign_spend == 0


def test_bind_principal_never_merges_a_bucket_twice(clock):
    b1 = B.budget_for(HOST, "k1", system="tracker", instance="one")
    b2 = B.budget_for(HOST, "k2", system="tracker", instance="two")
    b2.served["routine"] = 7
    B.bind_principal(b1, "user-9")
    merged = B.bind_principal(b2, "user-9")
    assert merged is b1 and b1.served["routine"] == 7
    # a second adapter still holding the stale b2 object binds again
    assert B.bind_principal(b2, "user-9") is b1
    assert b1.served["routine"] == 7               # not counted twice


def test_detach_prunes_buckets_nothing_uses(clock):
    old = B.budget_for(HOST, "rotated-key", system="tracker", instance="one")
    kept = B.budget_for(HOST, "shared-key", system="tracker", instance="one")
    B.budget_for(HOST, "shared-key", system="tracker", instance="two")
    B.detach("one")
    assert old.label not in B.snapshot_all()          # gone from /health
    assert kept.instances == {"two"}                  # still used by "two"
    assert "rotated-key" not in B._tokens             # credential forgotten
    assert "shared-key" in B._tokens
    # a bucket still blocked by a vendor rejection survives the prune
    blocked = B.budget_for(HOST, "blocked-key", system="tracker", instance="x")
    blocked.observe(B.RateSignal(limited=True, retry_after_s=60), now=clock.t)
    B.detach("x")
    assert blocked.label in B.snapshot_all()


# ── refill semantics: leaky bucket vs fixed window ───────────────────────────

def test_continuous_bucket_is_governed_by_the_reserve_alone(clock):
    """Linear refills continuously: a bucket at 30 % must still serve routine
    calls — the bucket size is the burst allowance, no pace line applies."""
    b = bucket(clock, **leaky(clock, 2500, 750))
    send, calls = sender(resp())
    run(b.request(send, reader(), instance="a"))
    assert calls == [1] and clock.sleeps == []
    # but the reserve still holds
    b.observe(B.RateSignal(limit=2500, remaining=300, refill="continuous"),
              now=clock.t)
    with pytest.raises(PMOBudgetExceeded):
        run(b.request(send, reader(), instance="a"))


def test_fixed_window_does_not_refill_before_its_reset(clock):
    """GitHub at zero with the reset 40 min away: no phantom refill, so a
    critical call is refused up front (its wait would exceed the budget)
    instead of being sent to a certain rejection."""
    b = bucket(clock, limit=5000, remaining=0, reset_at=clock.t + 2400,
               window_s=3600, refill="window")
    send, calls = sender(resp())
    clock.t += 600
    assert b._remaining_est(clock.t) == 0.0
    with pmo_call("critical"):
        with pytest.raises(PMOBudgetExceeded) as ei:
            run(b.request(send, reader(), instance="a"))
    assert calls == [] and ei.value.retry_after == pytest.approx(1800.0)


def test_fixed_window_starts_full_after_its_reset(clock):
    b = bucket(clock, limit=5000, remaining=0, reset_at=clock.t + 60,
               window_s=3600, refill="window")
    clock.t += 120
    assert b._remaining_est(clock.t) == 5000.0
    send, calls = sender(resp())
    run(b.request(send, reader(), instance="a"))
    assert calls == [1]


def test_foreign_spend_is_reported_per_hour_of_our_own_clock(clock):
    b = bucket(clock, **leaky(clock, 1000, 900))
    send, _ = sender(resp(), resp())
    run(b.request(send, reader(B.RateSignal(limit=1000, remaining=849,
                                             refill="continuous")),
                  instance="a"))
    assert b.foreign_spend == 50
    clock.t += 3601                                   # a new hour of ours
    run(b.request(send, reader(B.RateSignal(limit=1000, remaining=848,
                                             refill="continuous")),
                  instance="a"))
    assert b.foreign_spend == 0
