# ADR-0028 — The composition root becomes a factory

- **Status:** accepted (2026-08-04). Amends ADR-0015 (which fixed WHERE
  behavior lives; this fixes WHEN objects are built).
- **Context:** `api/main.py` constructed ~18 mutable singletons as module
  globals **at import time** — including `load_config()` (which WRITES
  config.yaml), template seeding (/data writes), `setup_telemetry()` (a
  global TracerProvider + a BatchSpanProcessor worker thread + httpx
  instrumentation), and live adapter clients — with late-binding hacks for
  cycles (`_mission_owner_of` forward-referencing a `poll_rt` defined 95
  lines later). Importing the module WAS half-booting the app: tests
  monkeypatched module globals, no two isolated instances could exist, and
  the 2026-08 evaluation found the composition root NEVER instantiated by
  any test. Nothing inside app/devcake imports api.main (only the
  Dockerfile CMD); exactly six test files did, five of them by patching
  globals.

## Decision

### 1 — `api/services.py`: the graph and its factory

A plain `Services` dataclass (deliberately NOT frozen, NOT slotted — tests
stub methods as instance attributes) holds every live object; the moved
module methods (`build_managers`, `reload_connections`,
`refresh_forge_health`, `managers_in_config_order`) become methods on it.
`build_services()` is the moved module body, with one structural
improvement: `poll_rt` is built BEFORE the `BlockerLocator`, so the
locator's owner lookup closes over the constructed runtime and the
`_mission_owner_of` late-binding hack dissolves. Two ADR-0015 invariants
carry over verbatim and are documented in the module: hot reload MUTATES in
place (config setattr, in-place manager reconcile, stable shared-dict
identities), and `poll_rt` is constructed once, never rebuilt on reload.

### 2 — main.py: wiring-only module scope + `svc()`

"Side-effect-free import" is an ALLOWLIST, not purity: `FastAPI()`,
middleware attach, `FastAPIInstrumentor.instrument_app`, logging setup and
`trace.get_tracer` (ProxyTracer — resolves the provider per span, the
api/poll.py precedent) legitimately run at import. The real invariant: no
/data reads-that-write or writes, no adapter/client construction, no global
telemetry install, no threads. Routes reach the graph through
`svc()` — inline it costs zero statements, so the ADR-0015 ≤4-statement
ratchet survives with its allowlist UNCHANGED; multi-service routes open
with `s = svc()`. Route names, signatures, and docstrings are unchanged
(the forwards are the test seam).

Lifespan order: credentials check → password floors → `setup_telemetry()`
(BEFORE the graph — httpx instrumentation must wrap the adapter clients;
now idempotent, since TestClient lifespans re-enter it) →
`services = build_services()` → secrets registration → template seeding
(moved from import) → the unchanged boot sequence → the task trio.
`services` stays bound after shutdown.

### 3 — Enforcement (test_structure_guards)

- `test_importing_main_is_side_effect_free`: subprocess import against a
  hostile env (ADMIN_* unset, fresh empty DEVCAKE_DATA_DIR) — must exit 0
  and leave the dir EMPTY.
- `test_main_module_level_is_wiring_only`: AST allowlist over top-level
  calls — catches what the filesystem probe can't (a re-added
  `Messaging(...)` at module scope writes nothing).
- The forge-sweep lifespan guard now matches attribute calls too
  (`s.refresh_forge_health()` / `refresh_all`) — a Name-only guard would
  have gone blind the day the sweep became a method.

### 4 — Tests: `fakes.make_services`

Five test files traded their module-global monkeypatch clusters for ONE
graph install: `monkeypatch.setattr(app_main, "services",
make_services(config=..., dev_types=..., ...))`. Unwired slots are
explode-on-touch sentinels that NAME the missing slot. The old globals are
DELETED, so a stale patch fails loudly (no shims). `make_services` never
calls the real factory — that reads /data and writes config.yaml.

## Consequences

- Two isolated app instances are now constructible; the composition root
  is exercisable in-process (a TestClient lifespan runs the real factory).
- **Accepted behavior shift (release note):** a corrupt config.yaml crashes
  at STARTUP, not at import — same fail-loudly, new log position.
- An authenticated request before the lifespan completes raises `svc()`'s
  named RuntimeError (→500). Unreachable in prod (uvicorn serves only
  after startup); intended in tests.
- Source-pinned wiring guards (shared blocker locator, real
  WorkspaceStore) now point at services.py.
- main.py: 883 → 785 lines (services.py holds the moved 234), and main's
  40+ dead legacy imports (pre-ADR-0015 vestiges F401 would have flagged)
  are gone.
