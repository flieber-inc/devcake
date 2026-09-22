# ADR-0044 — Deadlines never cancel a socket holder

- **Status:** accepted (2026-09-22)
- **Amends:** ADR-0034 (chokepoints: one sanctioned way to bound a wait),
  ADR-0040 (the request budget's probes ride the same rule)
- **Ticket:** none (field incident)

## Context

The app keeps one process-wide HTTP connection pool (`adapters/http`,
64 connections). Several callers bounded their waits on outbound calls
with `asyncio.timeout(...)`: the health builder's branch-protection walk
(one probe per work repository, 5 s each), its tracker probe (5 s), the
poll loop's forge sweep budget (60 s) and the blocker locator's peer
reads (5 s). A deadline of that kind **cancels** the coroutine it wraps.

httpcore's TLS handshake (`AnyIOStream.start_tls`) cleans up its TCP
stream on `Exception` only. A `CancelledError` is not one, so a cancel
that lands during the handshake leaves the socket open with nothing
referencing it; the pool records a failed connect and moves on. The far
end closes the idle socket minutes later (Cloudflare after 400 s), and
the kernel keeps it in `CLOSE_WAIT` for the life of the process.

On a two-core host running five Dev containers at once, the event loop
was starved: the branch-protection walk over 162 repositories took
minutes instead of seconds and most probes hit their 5 s cancel — many
of them mid-handshake. Tracing the reproduction, every leaked socket was
a request whose last event was the handshake (36 of 36). On
2026-09-21 the pile reached the pool's cap and every tracker and forge
call failed with a pool timeout for 40 minutes; finalizes deferred,
boards were skipped, nothing dispatched. A restart cleared it.

Two things were wrong at once: a host-sizing problem (its own change)
and a cancellation discipline the code never stated.

## Decision

1. **An outbound HTTP coroutine is bounded only by httpx's own timeouts.**
   Those raise ordinary exceptions and close their connection.
2. **A caller-side deadline waits, it never cancels.** `devcake.deadline`
   is the one sanctioned implementation: `bounded(aw, timeout)` runs the
   work as a strongly referenced task, waits on a *shielded* view of it
   and, on expiry, returns `pending` while the task finishes in the
   background. Its late outcome is retrieved and logged, never raised
   into nobody. `spawn(aw)` is the same without a wait. A task handed
   back in is reused, which is how a caller that missed its deadline
   waits on the same work next time (single-flight).
3. **Callers say "pending", not "failed".** The tracker probe writes a
   `probe pending` row (`ok: null`) that the finished probe replaces;
   the branch-protection walk runs as a background single-flight refresh
   and `/health` serves the last landed map (empty until the first one);
   the forge sweep is one in-flight task the next cycle waits on with a
   fresh budget; a slow peer read counts as a miss for that resolution
   only.
4. **The structure guard keeps it so.** `test_deadlines_never_cancel_a_socket_holder`
   walks the package for `asyncio.timeout` / `wait_for` (both spellings)
   and allows exactly four files, one reason each: the subprocess runner
   (a cancel kills the process group), the in-process SSE queue, the
   shutdown drain, and `deadline.py` itself.
5. **Shutdown is the one sanctioned cancel.** The lifespan drains
   pending waits for a few seconds and cancels the rest right before it
   closes the pool; a leaked socket dies with the process.
6. **Visibility.** `/health.http_pool` reports the pool's connections
   next to the kernel's `:443` sockets by state and the excess as a
   leaked-socket estimate, plus the count of background waits; `devcake
   status` prints it and the admin Overview alerts as the pile grows
   (warning) and near the cap (critical). A network error with no
   message names its class (`network: PoolTimeout`), so the log reads.

## Alternatives rejected

- **Patch httpcore's handshake cleanup locally** (`except BaseException`
  in a vendored copy or a monkeypatch). It fixes one leak site inside a
  dependency and leaves the discipline unstated; the next cancel-shaped
  wait finds the next leak. The upstream fix is still worth filing.
- **Longer per-probe timeouts.** They shrink the window, they do not
  close it; under starvation any finite cancel lands somewhere.
- **Cancel, then close the pool's client.** Closing a client mid-flight
  cancels every other in-flight request on it; a broader version of the
  same bug.

## Consequences

- The first `/health` after boot or a config reload carries an empty
  `forge_protection` map; the unprotected-branch advisory appears one
  refresh later (seconds on a healthy host). A refresh that started
  before a config reload finishes late with rows for the old card set;
  it discards them (a reload generation counter) and the next call
  starts a fresh walk.
- Background work is bounded by the adapter timeouts it carries (20 s
  per request): at most one refresh walk, one tracker probe per instance
  per minute, one sweep, and one peer read per resolution can be running
  past their callers. `/health.http_pool.background` shows the count.
- A slow tracker no longer paints red while it is merely slow: `ok: null`
  with `probe pending` is the honest state, per instance and in the
  `pmo` aggregate the health dot reads; a failure still paints red.
- `drain()` cancels what is left at shutdown and waits a short grace; a
  task that swallows its cancellation is logged and left behind, never
  allowed to hold the process.
- `leaked_estimate` counts every `:443` socket in the container's
  namespace, git-over-https children during a mirror sync included — a
  heuristic that spikes briefly during syncs, not a ledger.
