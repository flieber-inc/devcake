# ADR-0040 — PMO request budget: one governor, vendor headers stay in adapters

## Context — quota is a shared, metered resource the app could not see

Issue trackers meter API use. Linear allows a fixed number of requests and
of query-complexity points per hour **per user** — every API key minted by
one person draws on the same bucket, across deployments — and refills it
continuously. GitHub meters per user per hour with a fixed window and adds
secondary limits with a retry-after hint. GitLab throttles per minute.
Gitea publishes no quota, but a reverse proxy in front of it may.

DevCake's PMO adapters each had exactly one wire chokepoint (ADR-0034) but
no quota awareness: no header was read, a rejection surfaced as a transient
error, the poll skipped that instance for one tick and re-hammered the
vendor a tick later, and every rejection landed on whichever instance was
polled last. Write-back traffic (dispatch reads, finalize comments, labels,
uploads) grows with the size of the work, not with the poll interval, so a
large decomposition can exhaust a bucket at any cadence. Worst, a finalize
whose write was rejected was redelivered a handful of times and then
dead-lettered — a run's transcript, reply and stage transition lost to
nothing but quota. "Rate limits are comfortable" (ADR-0003) stopped being
true the day the vendor halved them.

## Decision

### 1 — One governed path per adapter (an ADR-0034 amendment, not an exception)

`adapters/budget.py` is the single request governor. Every PMO adapter's
wire call — the hot chokepoint and the per-call upload/download paths that
hit the API host — runs through `RequestBudget.request(send, read)`. The
governor imports no vendor; each adapter owns a `rate_signal(response)`
mapper that turns ITS headers and bodies into the neutral `RateSignal`
(limit, remaining, reset, window, rejected?, retry-after, a secondary
per-user fraction). docs/15's "no in-adapter retry ladder" stays the rule
for vendors: the one governed retry lives in one place, is bounded, and is
identical for every system.

### 2 — Budget identity is the credential on a host; Linear merges by user

A bucket is keyed by `(host, token(credential))` — an opaque per-process
token, never a digest of the credential's bytes — independent of
adapter objects, so a config-reload rebuild re-attaches to the same bucket
and any adapter on the same token (two instances on one key today; a
forge and an issue tracker on one token once forge calls are routed
through the governor) share it. The system name is a
label. Linear's per-user rule is honoured for free: the `viewer` id rides
the team query the adapter already makes, and `bind_principal` merges the
buckets of every key that belongs to that user. Other vendors are not
probed for identity — headers overwrite the remaining count on every
response, so two tokens of one user pace slightly optimistically, never
wrongly.

### 3 — Two call classes and the reserve

The port declares urgency through `pmo_call(class)` (a context, so the 17
port methods are untouched): **critical** = anything that writes a run's
results back or launches work — finalize, dispatch, operator actions —
and **routine** = everything else: poll reads, sweeps, probes. A fixed
share of the limit is reserved: routine calls are refused once the
estimate reaches it; critical calls may spend it. An undeclared caller is
routine, so nothing gains the reserve by omission.

### 4 — Pace line; routine never sleeps; one retry only after a definitive rejection

Headers are authoritative: every observation overwrites the local
estimate. Each adapter declares its vendor's refill semantics. A
continuously refilling bucket (Linear's leaky bucket) gains limit/window
tokens per second between observations and is governed by the reserve
alone: the bucket size is the burst allowance and the refill is the
pacing, so a pace line would only refuse calls the vendor would accept. A
fixed window (GitHub, GitLab) stays frozen until its reset and its routine
calls are spread along a pace line from the limit down to the reserve at
the reset, with a burst allowance so an under-budget host never notices.
A routine call that would have to wait is refused with
`PMOBudgetExceeded` (a `PMOTransient`, so the poll's segment-skip
semantics are unchanged) — the poll interval is its pacing clock. Critical calls wait for the refill under one cumulative deadline
per call context (a finalize's many calls share it) and are retried
**once** after a definitive rejection: a 429 or RATELIMITED response was
never executed, so the retry is idempotent. Network errors and timeouts
are never retried by the governor — the request may have been applied.

### 5 — Transient finalize failures never poison

The ingress consumer counts handling failures toward its dead-letter
threshold only for permanent errors. A transient failure leaves the entry
pending for reclaim, where the critical class makes the retry wait for
quota instead of failing again. A ceiling remains: an entry that has been
failing transiently for three hours — longer than any vendor quota window,
no longer than the run-timeout plus stall-grace envelope a finalizing run
could already occupy a concurrency slot for — is dead-lettered with that
reason, so an unreachable tracker cannot pin an entry and its slot.

### 6 — Visibility

`/health` carries one row per bucket (`pmo_budget`: limit, remaining,
reset, waits, refusals, rejections seen, demand per hour per instance,
quota spent by someone else on the same credential) and an advisory
(`pmo_budget_warnings`) when measured demand exceeds a share of the limit,
naming the poll interval that would fit and the instances sharing the
bucket. The admin derives a dismissable warning from it. Waits of a second
or more emit a `pmo.budget.wait` span.

### 7 — A switch for rollout

`DEVCAKE_PMO_BUDGET_OFF=1` keeps the governor observing and reporting
without waiting or refusing, so an operator can read a host's real demand
before pacing changes its behaviour.

## Alternatives rejected

- **Webhooks as the fix.** They remove the periodic read, not the
  write-back traffic that grows with work; they need a public ingress
  private hosts lack, retried deliveries still require a periodic full
  read, and an inbound door on the control plane must verify signatures
  and survive abuse. They remain a later wake-up seam on top of this
  governor, never a substitute for it.
- **Per-vendor retry ladders in each adapter.** Four drifting copies with
  no cross-instance view — the drift ADR-0034 exists to prevent.
- **A five-class priority ladder with per-class deadlines.** Policy
  without evidence; the reserve is the mechanism, two classes suffice.
- **Identity probes for every vendor.** Extra requests for marginal
  precision; headers already correct the estimate on every response.
- **Redis-backed failure counters or a shared bucket.** The app is one
  process; a transient flag plus an age ceiling is enough, and the
  over-count of "several transient deliveries then one permanent" is a
  documented edge, not a mechanism.
- **New configuration knobs.** Every field forces contract regeneration
  across the admin; constants plus one rollout switch suffice.
- **Rotating the poll order for fairness.** Config order also drives
  cross-instance ownership claims; it must stay stable. The advisory,
  not scheduling tricks, is the remedy when demand exceeds the budget.

## Consequences

- docs/15 §2 (`PMO_TRANSIENT` row, §5 poison rule), docs/05 §2/§2a/§7/§8,
  docs/04 §1, docs/09 §4, docs/11 (health rows, alert), docs/12 (span),
  docs/13 (switch) describe the behaviour normatively.
- ADR-0003's "rate limits are comfortable" is retired: demand is governed
  and measured; enumeration reads that can reuse a cycle's board fetch are
  a separate change with its own doctrine amendment.
- Forge adapters keep their own transient handling for now; because the
  bucket is keyed by credential on host, routing their wire calls through
  the same governor later needs no key change.
- The governor is per process; a multi-process deployment would split a
  credential's bucket across processes and is out of scope.
