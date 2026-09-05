"""PMO request budget — ONE governor in front of every PMO adapter's wire
call (ADR-0040; the chokepoint discipline of ADR-0034 applied to quota).

Issue trackers meter requests per credential or per user (Linear: every API
key of one user shares a single bucket). Before this module the adapters
raised `PMOTransient` on a rejection and the poll re-hammered the vendor a
tick later; a run's finalize could be dead-lettered on nothing but quota.

Design (docs/15 §2, docs/05 §2a):
- Vendor knowledge stays in the adapters: each maps its response headers
  and bodies into a `RateSignal`. This module knows no vendor.
- One `RequestBudget` per credential on a host (`budget_for`), independent
  of adapter objects, so a config-reload rebuild re-attaches to the same
  bucket. The credential is identified by an opaque per-process token, never
  by a digest of its bytes. Linear additionally merges buckets by user
  (`bind_principal`).
- Headers are authoritative: every observation overwrites the local
  estimate. The vendor's refill semantics decide what happens between
  observations: a continuously refilling bucket (Linear's leaky bucket)
  gains limit/window tokens per second and is governed by the reserve
  alone — the bucket size IS the burst allowance; a fixed window (GitHub,
  GitLab) stays frozen until its reset and is paced along a line from the
  limit down to the reserve at the reset.
- Two call classes (ports/pmo.py `pmo_call`): `critical` may spend the
  reserve and WAITS for the refill (bounded by a cumulative deadline),
  retrying ONCE after a definitive rejection — a 429/RATELIMITED response
  was never executed, so the retry is idempotent; network errors are never
  retried here. `routine` is paced and NEVER sleeps: it is refused with
  `PMOBudgetExceeded` before any vendor call, and the poll interval is its
  pacing clock.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx
from opentelemetry import trace

from ..ports.pmo import PMOBudgetExceeded, PMOTransient, pmo_call_ctx
from ..activity import IN_FLIGHT

log = logging.getLogger("devcake.adapters.budget")
tracer = trace.get_tracer("devcake")

# Fraction of the limit kept for critical calls — routine calls are refused
# once the estimate dips into it.
RESERVE = 0.15
# Burst allowance ahead of the pace line (fraction of the limit) so a host
# comfortably under budget never waits.
AHEAD = 0.10
# Cumulative wait a critical call context may spend, when the caller sets none.
CRITICAL_WAIT_S = 120.0
DEFAULT_WINDOW_S = 3600
# A rejection with no vendor hint blocks the bucket this long.
FALLBACK_BLOCK_S = 5.0
# Waits shorter than this are not traced (noise).
WAIT_SPAN_MIN_S = 1.0
# Demand is reported only after this much observation (noise floor).
DEMAND_MIN_OBSERVED_S = 300.0
# Kill switch for rollout: observe and report, never wait or refuse.
_OFF = os.environ.get("DEVCAKE_PMO_BUDGET_OFF", "").strip().lower() in (
    "1", "true", "yes", "on")

# monkeypatch seams (tests drive a fake clock; nothing here sleeps for real)
_clock = time.time
_mono = time.monotonic
_sleep = asyncio.sleep

CLASSES = ("critical", "routine")


def header_int(headers, name: str) -> int | None:
    """Tolerant header read for the adapters' signal mappers."""
    v = headers.get(name)
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def header_float(headers, name: str) -> float | None:
    v = headers.get(name)
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RateSignal:
    """What ONE vendor response says about the quota. Every field optional:
    a vendor without headers yields `RateSignal()` and stays unpaced until
    a rejection arrives."""
    limit: int | None = None
    remaining: int | None = None
    reset_at: float | None = None          # epoch seconds
    window_s: int | None = None            # vendor window; None keeps the last
    limited: bool = False                  # THIS response was a rate-limit rejection
    retry_after_s: float | None = None     # vendor's hint, seconds
    complexity_fraction: float | None = None   # secondary per-user bucket, 0..1
    endpoint: dict | None = None           # advisory only, never paced on
    refill: str | None = None              # "continuous" (leaky bucket) | "window" (fixed)


class _Meter:
    """Per-instance demand meter: one-minute bins over the last hour."""

    def __init__(self) -> None:
        self._bins: deque[tuple[int, int]] = deque()
        self._born: float | None = None

    def tick(self, now: float) -> None:
        if self._born is None:
            self._born = now
        minute = int(now // 60)
        if self._bins and self._bins[-1][0] == minute:
            self._bins[-1] = (minute, self._bins[-1][1] + 1)
        else:
            self._bins.append((minute, 1))
        while self._bins and self._bins[0][0] < minute - 59:
            self._bins.popleft()

    def count(self, now: float) -> int:
        """Raw events in the last hour — no extrapolation (rejections)."""
        minute = int(now // 60)
        return sum(c for m, c in self._bins if m >= minute - 59)

    def absorb(self, other: "_Meter") -> None:
        """Fold another meter's bins in (bucket merge by principal)."""
        merged: dict[int, int] = {}
        for m, c in list(self._bins) + list(other._bins):
            merged[m] = merged.get(m, 0) + c
        self._bins = deque(sorted(merged.items()))
        borns = [b for b in (self._born, other._born) if b is not None]
        self._born = min(borns) if borns else None

    def per_hour(self, now: float) -> int | None:
        if self._born is None:
            return None
        observed = min(now - self._born, 3600.0)
        if observed < DEMAND_MIN_OBSERVED_S:
            return None
        minute = int(now // 60)
        total = sum(c for m, c in self._bins if m >= minute - 59)
        return int(round(total * 3600.0 / observed))


class RequestBudget:
    """One vendor quota bucket. Not a lock: evaluation and the local spend
    increment are synchronous, so concurrent waiters cannot double-spend."""

    def __init__(self, key: tuple[str, str], *, host: str, fingerprint: str,
                 system: str) -> None:
        self.key = key
        self.host = host
        self.fingerprint = fingerprint
        self.fingerprints = {fingerprint}
        self.systems = {system}
        self.instances: set[str] = set()
        self.principal: str | None = None
        self.limit: int | None = None
        self.remaining: int | None = None
        self.reset_at: float | None = None
        self.window_s: int = DEFAULT_WINDOW_S
        self.refill = "window"
        self.blocked_until = 0.0
        self.complexity_fraction: float | None = None
        self.endpoint: dict | None = None
        self.last_observed_at: float | None = None
        self._local_spent = 0
        self._served_since_observed = 0
        self.foreign_spend = 0      # quota others spent in the current hour
        self._foreign_window_end = 0.0
        self.served = {c: 0 for c in CLASSES}
        self.rejections = {c: 0 for c in CLASSES}
        self.waits = 0
        self.wait_seconds = 0.0
        self.limited_seen = 0
        # the vendor's own rejections over the last hour — the loud tier
        # (self-throttle stays in `rejections`: the governor working)
        self._limited = _Meter()
        self.last_limited_at: float | None = None
        self._meters: dict[str, _Meter] = {}

    # ── identity ──
    @property
    def label(self) -> str:
        who = self.principal or f"key-{self.fingerprint[:6]}"
        return f"{self.host}/{who}"

    # ── observation ──
    def observe(self, sig: RateSignal, *, now: float | None = None) -> None:
        now = _clock() if now is None else now
        if sig.window_s:
            self.window_s = int(sig.window_s)
        if sig.limit is not None:
            self.limit = int(sig.limit)
        if sig.refill in ("continuous", "window"):
            self.refill = sig.refill
        if sig.reset_at is not None:
            self.reset_at = float(sig.reset_at)
        if now >= self._foreign_window_end:
            # foreign spend is reported per rolling window of our own clock —
            # vendor reset semantics differ (a fixed boundary vs "when full")
            self.foreign_spend = 0
            self._foreign_window_end = now + self.window_s
        if sig.remaining is not None:
            prev, spent = self.remaining, self._served_since_observed
            if prev is not None and self.limit:
                # what the vendor should report if only WE spent since the
                # last observation. No refill term: a refill can only raise
                # the count, so leaving it out under-counts foreign spend
                # but never invents it on a fixed-window vendor
                expected = prev - spent
                if sig.remaining < expected - 2:
                    self.foreign_spend += int(expected - sig.remaining)
            self.remaining = int(sig.remaining)
            self._local_spent = 0
            self._served_since_observed = 0
        if sig.complexity_fraction is not None:
            self.complexity_fraction = max(0.0, min(1.0, float(sig.complexity_fraction)))
        if sig.endpoint:
            self.endpoint = dict(sig.endpoint)
        if sig.remaining is not None or sig.limit is not None:
            self.last_observed_at = now
        if sig.limited:
            self.limited_seen += 1
            self._limited.tick(now)
            self.last_limited_at = now
            if sig.retry_after_s is not None:
                block = float(sig.retry_after_s)
            elif sig.remaining == 0 and self._ttr(now) is not None:
                block = self._ttr(now) or 0.0
            elif self.limit:
                block = self.window_s / self.limit
            else:
                block = FALLBACK_BLOCK_S
            self.blocked_until = max(self.blocked_until, now + max(block, 0.0))

    def _ttr(self, now: float) -> float | None:
        """Seconds to the vendor's reset, clamped to [0, window]; a reset
        more than one window in the past means the value is stale (clock
        skew or a dead header) and is treated as unknown."""
        if self.reset_at is None:
            return None
        ttr = self.reset_at - now
        if ttr < -self.window_s:
            return None
        return min(max(ttr, 0.0), float(self.window_s))

    def _remaining_est(self, now: float) -> float | None:
        if self.limit is None or self.remaining is None:
            return None
        elapsed = max(now - (self.last_observed_at or now), 0.0)
        if self.refill == "continuous":
            refill = self.limit * elapsed / self.window_s
            est = self.remaining - self._local_spent + refill
            if self.complexity_fraction is not None:
                frac = min(1.0, self.complexity_fraction + elapsed / self.window_s)
                est = min(est, self.limit * frac)
        else:
            # a fixed window stays frozen until its reset, then starts full
            reset_passed = (self.reset_at is not None and now >= self.reset_at
                            and self.last_observed_at is not None
                            and self.last_observed_at < self.reset_at)
            base = float(self.limit) if reset_passed else float(self.remaining)
            est = base - self._local_spent
            if self.complexity_fraction is not None and not reset_passed:
                est = min(est, self.limit * self.complexity_fraction)
        return max(min(est, float(self.limit)), 0.0)

    # ── decision ──
    def _decide(self, now: float, critical: bool) -> tuple[float, str]:
        """(seconds to wait, reason). 0 = go."""
        if self.blocked_until > now:
            return self.blocked_until - now, "vendor rate limit in effect"
        if self.limit is None:
            return 0.0, ""
        rem = self._remaining_est(now)
        if rem is None:
            return 0.0, ""
        rate = self.limit / self.window_s          # tokens per second
        continuous = self.refill == "continuous"
        ttr = self._ttr(now)
        if critical:
            if rem >= 1.0:
                return 0.0, ""
            if continuous or ttr is None:
                return (1.0 - rem) / rate, "quota exhausted; waiting for the refill"
            return max(ttr, 1.0), "quota exhausted; waiting for the window to reset"
        floor = math.ceil(self.limit * RESERVE)
        if rem <= floor:
            if continuous or ttr is None:
                wait = (floor - rem + 1.0) / rate
            else:
                wait = max(ttr, 1.0)
            return wait, "remaining quota is reserved for critical calls"
        if continuous:
            # the bucket size is the burst allowance and the refill IS the
            # pacing: nothing to spread, the reserve alone governs
            return 0.0, ""
        if ttr is not None and self.limit > floor:
            line = floor + (self.limit - floor) * ttr / self.window_s
            allowance = self.limit * AHEAD
            if rem < line - allowance:
                return (line - allowance - rem) / rate, "ahead of the pace line"
        return 0.0, ""

    # ── the governed call ──
    async def request(self, send: Callable[[], Awaitable[httpx.Response]],
                      read: Callable[[httpx.Response], RateSignal], *,
                      instance: str = "", op: str = "") -> httpx.Response:
        """Run ONE governed vendor call. `op` prefixes the rejection message
        so an adapter's error wording stays its own (e.g. "vendor download →")."""
        ctx = pmo_call_ctx.get()
        cls = ctx.call_class if ctx is not None else "routine"
        critical = cls == "critical"
        if instance:
            self.instances.add(instance)
        # the wait budget bounds time spent WAITING for quota across the
        # whole call context (a finalize's many calls); other work inside the
        # context — a merge, a clone — never eats it
        budget = (ctx.wait_budget_s if ctx is not None and ctx.wait_budget_s is not None
                  else CRITICAL_WAIT_S)
        waited = ctx.waited_s if ctx is not None else 0.0
        attempts = 0
        rejected_by_vendor = False
        while True:
            now = _clock()
            wait, why = (0.0, "") if _OFF else self._decide(now, critical)
            if wait > 0:
                if not critical:
                    self.rejections[cls] += 1
                    self._note_refusal(cls, why)
                    raise PMOBudgetExceeded(
                        f"{op}request budget ({self.label}): {why}",
                        retry_after=wait, reset_at=self.reset_at)
                if waited + wait > budget:
                    self.rejections[cls] += 1
                    self._note_refusal(cls, why)
                    if rejected_by_vendor:
                        raise PMOTransient(
                            f"{op}rate limited by {self.label}",
                            retry_after=wait, reset_at=self.reset_at)
                    raise PMOBudgetExceeded(
                        f"{op}request budget ({self.label}): {why}; wait "
                        f"{wait:.0f}s exceeds the call deadline",
                        retry_after=wait, reset_at=self.reset_at)
                await self._wait(wait, cls, instance, why)
                waited += wait
                if ctx is not None:
                    ctx.waited_s = waited
                continue
            self._local_spent += 1
            self._served_since_observed += 1
            self.served[cls] += 1
            self._meters.setdefault(instance or "-", _Meter()).tick(now)
            resp = await send()
            sig = read(resp)
            self.observe(sig, now=_clock())
            if not sig.limited:
                return resp
            rejected_by_vendor = True
            if critical and attempts == 0 and not _OFF:
                attempts = 1
                continue
            raise PMOTransient(
                f"{op}rate limited by {self.label}",
                retry_after=max(self.blocked_until - _clock(), 0.0),
                reset_at=self.reset_at)

    async def _wait(self, wait: float, cls: str, instance: str, why: str) -> None:
        self.waits += 1
        self.wait_seconds += wait
        if wait < WAIT_SPAN_MIN_S:
            await _sleep(wait)
            return
        with tracer.start_as_current_span("pmo.budget.wait") as span, \
                IN_FLIGHT.phase("pmo.budget.wait", self.label,
                                expect_s=wait + 5, wait_s=round(wait, 1),
                                call_class=cls, instance=instance or "",
                                reason=why):
            span.set_attribute("devcake.instance", instance or "")
            span.set_attribute("devcake.pmo.call_class", cls)
            span.set_attribute("devcake.pmo.wait_s", round(wait, 3))
            span.set_attribute("devcake.pmo.remaining",
                               -1 if self.remaining is None else self.remaining)
            span.set_attribute("devcake.pmo.reason", why)
            log.info("pmo budget %s: %s call waits %.1fs (%s)",
                     self.label, cls, wait, why)
            await _sleep(wait)

    def _note_refusal(self, cls: str, why: str) -> None:
        span = trace.get_current_span()
        span.set_attribute("devcake.pmo.budget_refused", True)
        span.set_attribute("devcake.pmo.reason", why)
        log.debug("pmo budget %s: %s call refused (%s)", self.label, cls, why)

    # ── merge + report ──
    def merge(self, other: "RequestBudget") -> None:
        self.fingerprints |= other.fingerprints
        self.systems |= other.systems
        self.instances |= other.instances
        for c in CLASSES:
            self.served[c] += other.served[c]
            self.rejections[c] += other.rejections[c]
        self.waits += other.waits
        self.wait_seconds += other.wait_seconds
        self.limited_seen += other.limited_seen
        self._limited.absorb(other._limited)
        self.last_limited_at = max(
            (t for t in (self.last_limited_at, other.last_limited_at)
             if t is not None), default=None)
        self.foreign_spend += other.foreign_spend
        for name, meter in other._meters.items():
            self._meters.setdefault(name, meter)
        if (other.last_observed_at or 0) > (self.last_observed_at or 0):
            for attr in ("limit", "remaining", "reset_at", "window_s", "refill",
                         "complexity_fraction", "endpoint", "last_observed_at",
                         "_local_spent", "_served_since_observed"):
                setattr(self, attr, getattr(other, attr))
        self.blocked_until = max(self.blocked_until, other.blocked_until)

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        now = _clock() if now is None else now
        est = self._remaining_est(now)
        return {
            "label": self.label,
            "host": self.host,
            "principal": self.principal,
            "systems": sorted(self.systems),
            "instances": sorted(self.instances),
            "limit": self.limit,
            "remaining": self.remaining,
            "remaining_estimate": None if est is None else int(est),
            "reset_at": self.reset_at,
            "window_s": self.window_s,
            "refill": self.refill,
            "blocked_until": self.blocked_until if self.blocked_until > now else None,
            "complexity_fraction": self.complexity_fraction,
            "endpoint": self.endpoint,
            "served": dict(self.served),
            "rejections": dict(self.rejections),
            "waits": self.waits,
            "wait_seconds": round(self.wait_seconds, 1),
            "limited_seen": self.limited_seen,
            "limited_last_hour": self._limited.count(now),
            "last_limited_at": self.last_limited_at,
            "foreign_spend": self.foreign_spend,
            "observed_at": self.last_observed_at,
            "demand_per_hour": {name: m.per_hour(now)
                                for name, m in sorted(self._meters.items())},
        }


# ── registry (process-global, independent of adapter objects) ────────────────

_registry: dict[tuple[str, str], RequestBudget] = {}
_alias: dict[tuple[str, str], tuple[str, str]] = {}
# credential → opaque token (process-local, random): the bucket key must
# identify a credential without carrying or deriving from its bytes — no
# digest, so the token reveals nothing and is never usable as a credential
_tokens: dict[str, str] = {}


def fingerprint(credential: str | None) -> str:
    """Opaque, process-local identity of a credential: the same string maps
    to the same token for the life of the process (rebuilds re-attach);
    nothing about the token is derived from the credential's bytes."""
    cred = credential or ""
    tok = _tokens.get(cred)
    if tok is None:
        tok = secrets.token_hex(8)
        _tokens[cred] = tok
    return tok


def budget_for(host: str, credential: str | None, *, system: str,
               instance: str = "",
               registry: dict[tuple[str, str], RequestBudget] | None = None
               ) -> RequestBudget:
    """The bucket for one credential on one host — created on first use,
    shared by every adapter (any system, any instance) built on the same
    credential, re-attached across rebuilds."""
    reg = _registry if registry is None else registry
    host = (host or "").strip().lower() or "-"
    fp = fingerprint(credential)
    key = (host, fp)
    if registry is None:
        key = _alias.get(key, key)
    b = reg.get(key)
    if b is None:
        b = RequestBudget(key, host=host, fingerprint=fp, system=system)
        reg[key] = b
    b.systems.add(system)
    if instance:
        b.instances.add(instance)
    return b


def bind_principal(budget: RequestBudget, principal_id: str | None, *,
                   registry: dict[tuple[str, str], RequestBudget] | None = None
                   ) -> RequestBudget:
    """Re-key a bucket by the vendor USER it belongs to (Linear: every key
    of one user shares one quota). Buckets of two keys that turn out to
    belong to one user are merged — the newest observation wins. Returns
    the bucket the caller must use from now on."""
    if not principal_id:
        return budget
    reg = _registry if registry is None else registry
    pkey = (budget.host, f"user:{principal_id}")
    existing = reg.get(pkey)
    if (existing is not None and existing is not budget and registry is None
            and all(_alias.get((budget.host, fp)) == pkey
                    for fp in budget.fingerprints)):
        # a second adapter still holding a bucket that was already merged
        # into this principal — hand it the merged one, never merge twice
        return existing
    if existing is None or existing is budget:
        reg[pkey] = budget
        budget.principal = principal_id
        target = budget
    else:
        existing.merge(budget)
        target = existing
    for fp in budget.fingerprints:
        old = (budget.host, fp)
        if registry is None:
            _alias[old] = pkey
        if reg.get(old) is not target:
            reg.pop(old, None)
    return target


def detach(instance: str) -> None:
    """A manager rebuild (config reload) dropped or repointed an instance:
    forget the name on every bucket, then prune buckets nothing uses —
    a rotated or retired credential must not linger on /health, and its
    token must not stay in memory."""
    for b in _unique_budgets():
        b.instances.discard(instance)
        b._meters.pop(instance, None)
    prune()


def prune(*, now: float | None = None) -> None:
    now = _clock() if now is None else now
    dead = [b for b in _unique_budgets()
            if not b.instances and b.blocked_until <= now]
    for b in dead:
        for key in [k for k, v in _registry.items() if v is b]:
            _registry.pop(key, None)
        for key in [k for k, v in _alias.items() if v == b.key or k in
                    {(b.host, fp) for fp in b.fingerprints}]:
            _alias.pop(key, None)
    live = {fp for b in _unique_budgets() for fp in b.fingerprints}
    for cred in [c for c, tok in _tokens.items() if tok not in live]:
        _tokens.pop(cred, None)


def _unique_budgets() -> list[RequestBudget]:
    seen: dict[int, RequestBudget] = {}
    for b in _registry.values():
        seen.setdefault(id(b), b)
    return list(seen.values())


def snapshot_all(*, now: float | None = None) -> dict[str, dict[str, Any]]:
    return {b.label: b.snapshot(now=now) for b in _unique_budgets()}


def reset() -> None:
    """Tests only: forget every bucket, alias and credential token."""
    _registry.clear()
    _alias.clear()
    _tokens.clear()
