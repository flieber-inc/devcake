# ADR-0015 — Orchestrator module functions + API composition-root discipline

- **Status:** accepted (2026-07-18); **amended by ADR-0028** (composition-root
  *construction* moved to `api/services.build_services()`; the route-forward
  ratchet and ≤4-statement bodies remain). Ships as the C1–C7 series of the
  pre-v0.2 structural plan. C1 (this ADR + the binding-block removal + the
  structure guard test) is the normative anchor; C2/C3 finish the orchestrator,
  C4–C6 split the API composition root, C7 splits the admin ConfigPage.
- **Context:** ISSUES #36 split the ~1.8k-line orchestrator god module into
  `domain/orchestrator/` but left a *transitional façade*: ~50 free functions
  taking `self`, bound onto `MissionManager` by module-level assignment after
  the class body. Nothing was greppable as a method, `staticmethod()`/
  `classmethod()` wrappers hid calling conventions, and the test suite grew
  ~80 private-seam call sites (`mgr._transition(...)`) with no sanctioned
  public seam. Meanwhile `api/main.py` accreted to ~1.8k lines: composition
  root + poll loop + health probes + ~60 endpoint bodies. Both were flagged by
  the 2026-07-18 skeptical review as "file split, not modularity."

> **Amended by ADR-0028 (2026-08-04):** Decision 3's "main.py is composition
> root" still governs *where routes live* and the ≤4-statement forward ratchet
> (guard-tested residual count is authority — currently only `run_steward`;
> earlier drafts claimed nine then seven). **Object-graph construction** moved
> to `api/services.build_services()`; `main.py` is wiring + route forwards
> only. Read ADR-0028 for the factory seam; do not treat Decision 3 alone as
> current law on *where* dependencies are built.

## Decision 1 — the manager is DI container + advisory state + verbs

`MissionManager` holds: injected dependencies, flat advisory state, and
**explicit delegating methods** whose signatures mirror the module functions
exactly. Implementation lives in the sibling modules; module functions take
`mgr` (né `self`) as an explicit first parameter; cross-module calls become
explicit imports (acyclic: `finalize → transitions → {review, decomposition}`,
`sweeps → review`). Six cross-cutting primitives stay methods forever —
`_audit`, `_feed`, `_checkpoint`, `_trip_breaker`, `_run_is_ours`,
`_attachment_cap` — because fakes override them per instance
(`fakes.make_mission_manager` sets `mgr._audit`), `mission_actions` duck-types
`_audit`, and routing every module through the manager for these removes the
feed/finalize import fan-in that would otherwise cycle. **Never bind
attributes onto the class after its definition** — the façade mechanism is
forbidden and guard-tested (`tests/test_structure_guards.py`). No mixins; no
re-inlining bodies into one class.

## Decision 2 — advisory state stays flat on the manager

Reaffirms ADR-0009. No `AdvisoryState` wrapper object: `breakers` is an
injected dict aliased by `main.shared_breakers` and `OAuthManager`;
`anomalies`/`rearm_merge_repos`/`_grace` are mutated from the composition
root; and `build_managers()` reconciles managers **in place** on config reload
precisely so advisory state survives — the manager's identity IS the state
container. A wrapper is isomorphic to the manager and only endangers the
aliasing. Shared state is shared by injection, never duplicated. State-bearing
runtime objects (`FinalizerRouter`; `PollRuntime` from C4) hold the managers
dict by live reference and are never rebuilt on config reload.

## Decision 3 — main.py is composition root + route forwards, nothing else

> **Scope note (post ADR-0028):** this decision remains normative for *route
> placement and body size*. For *construction of the object graph*, the
> composition root is `api/services.build_services()` (ADR-0028) — `main.py`
> no longer builds managers/adapters as import-time globals.

Endpoint behavior lives in `api/` application-service modules (the
`mission_actions.py`/`clear.py` pattern: explicit parameters, raise
`HTTPException`, never import `main.py`). Route decorators stay in `main.py`
as forwards with **unchanged coroutine names and signatures** — tests call
them directly (`app_main.save_profile(...)`), so the forward layer is the
API's stable test seam. No `APIRouter`: it would be a second routing idiom for
zero behavior. Every `@app.<verb>` body is ≤ 4 statements, counted
**recursively** through compound bodies (`with`/`async with`/`try`/`if`/
nested `def`) and AST-guarded with an allowlist that may only shrink, never
grow. The live allowlist holds only `run_steward`; earlier residuals
(`dispatch_hello`, oauth/mapper trio, `get_run`/`get_run_log`/
`stream_run_log`/`clear_runs`) are thin forwards into service modules. The
guard test (`test_structure_guards.py`) is the source of truth — including
app-object name aliases (`api = app` then `@api.post`). Poll machinery lives
in `api/poll.py` as `PollRuntime`; health probes in `api/health.py`.

## Decision 4 — module-public functions are the sanctioned test seam

The dominant private seams are legitimized, not relocated:
`transitions.transition`, `sweeps.merge_sweep`/`tracking_sweep`,
`dispatch.attempt_number`/`resolve_repo`, `review.finalize_review`,
`decomposition.finalize_decomposition`, `mapper.apply_mapper_edges`
*(historical name — live public seams are `steward.apply_steward_edges` /
`finalize_steward` under `domain/orchestrator/steward.py`)* become
their modules' public functions and the tests call them as such (AGENTS.md "no
private tests / agree the seam first"). `_feed`/`_checkpoint` stay methods, so
their test sites stand.

## Rejected

- **Mixins / one big class** — re-creates the god module with
  multiple-inheritance opacity.
- **A separate deps/state object** — isomorphic to the manager (the functions
  need pmo, runs, config, forges *and* the advisory dicts); pure rename risk
  against `build_managers()` and `shared_breakers` aliasing.
- **APIRouter migration** — new idiom, breaks the direct-coroutine test seam,
  zero behavior gained.
- **Retargeting merge-sweep tests to `mgr.sweeps()`** — fabricated mission
  lists, weaker assertions, more churn than legitimizing the module function.

## Consequences

- The structure guard test is the ratchet: the façade cannot quietly return,
  and route bodies cannot quietly grow. Honest limit: module functions still
  receive the whole `mgr` — state access is *explicit and greppable*, not
  *narrow*. Per-module state slices were considered and rejected (Decision 2);
  revisit only if a real bug traces to over-broad state access.
- Behavior-preserving throughout: the full suite must stay green at every PR
  boundary; C-series diffs move code byte-identically wherever possible.
