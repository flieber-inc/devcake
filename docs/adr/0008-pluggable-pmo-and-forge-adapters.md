# ADR-0008 — Pluggable PMO and Forge Adapters

**Status:** accepted (2026-07-13); **partially superseded** — D4/D5/D7 and the multi-instance "future" consequence are struck (schema v4 / ADR-0009 / live `ForgeCapabilities` + `cancel_mission`). · **Relates to:** ADR-0003 (PMO as source of truth), ADR-0004 (label namespace), ADR-0006/0007 (Linear-specific policies)

> **Supersession note (runtime today — do not read D1 construction site / D4–D5 / D7 as current law):**
> multi-instance is **0..N** PMO and repos (ADR-0009 / schema v4); `ForgeCapabilities`
> **exists** as a ClassVar on each forge adapter; branch naming is
> **instance-prefixed** (`mission_branch(instance, key)` → `devcake/{INSTANCE}-{key}`,
> with `legacy_branch` for pre-v3 records); `cancel_mission` is on `PMOPort` and used
> by decomposition/sweeps. **Construction:** forge/PMO adapters are built only via
> `adapters/registry.py` factories (`make_forge`, `make_pmo`, `make_internal_forge`,
> `make_gitea_adapter`); composition injects them from `api/services.build_services()`
> (ADR-0028) — not by constructing vendor classes in `api/main.py`. Token redaction
> for configured repos registers at those factories; Gitea value-registration is
> ADR-0010. The historical narrative below records the ADR as accepted on 2026-07-13
> — leave it intact for provenance.

## Context

v0 shipped with Linear as the only PMO and GitHub/GitLab as duck-typed forge
twins. The PMO abstraction was half-built (a stale 10-method Protocol that
consumers bypassed by typing against `LinearAdapter` and reaching into private
methods); the forge had no port at all ("normalize to the GitHubForge dict
shape" was the de-facto contract); the dev-side forge dialect was
string-templated across `prompts.py`, the Dev entrypoint, and `security.py`;
and docs/01 described a hexagonal layout that did not exist. Future versions
must add PMO systems and forges **additively** — multiple of each per
instance, eventually — without touching the core.

## Decisions

1. **Hexagonal tree, for real.** The code now matches docs/01 §3:
   `domain/` (pure logic, depends on port Protocols), `ports/` (`PMOPort`,
   `ForgePort` + boundary DTOs; later also run-infrastructure ports — see
   **Follow-up** below), `adapters/` (`linear/`, `github/`, `gitlab/`, `dagu/`,
   `files/`, `redis/`, and `registry.py`), `api/`, `telemetry/`, `prompts/`.
   `config.py`, `security.py`, `harness.py` stay at the package root as
   cross-cutting concerns. Adapter **construction** lives in `api/main.py`
   via the registry; the domain receives adapters fully built. *As accepted
   (2026-07-13):* `ExecutorPort`/`StatePort` were deliberately deferred
   (adapters packaged; Protocols roadmap).

2. **One authoritative `PMOPort`, MissionRef-unified.** The port's reads and
   writes are keyed by `MissionRef(pmo_id, kind)`; the adapter dispatches
   issue-vs-project internally (`get`, `get_activity`, `children_of`,
   `post_feed`, `set_status`, `swap_labels`). *Alternative considered:*
   capability-driven dispatch (domain consults `capabilities()` and picks
   per-kind methods) — rejected because it keeps Linear's duality in the
   domain forever and forces every future PMO to expose a duality it may not
   have. `pmo_kind` stays on the `Mission` DTO (derivation and ADR-0006 need
   it); only the storage mechanics moved. Attachment filenames are resolved by
   the adapter (`AttachmentRef{url, name}`) — the domain never parses vendor
   asset URLs. `PMOTransient` lives in the port module.

3. **Adapter registry + hot reload.** `adapters/registry.py` is the single
   place that knows which PMO systems and forges exist (`PMO_SYSTEMS`,
   `make_pmo`, `make_forge`, `forges()`), including each adapter's secret env
   vars, token regexes, paste-guard prefixes, capability residual flags, and
   launch-vs-experimental labeling (`PMOSystemInfo.experimental` — all four
   current systems are launch-supported; `True` is reserved for future
   opt-ins).
   Config `system`/`forge` fields are open strings validated against the
   registry (an unknown value 422s exactly like the old `Literal`s). A config
   PUT calls `reload_connections()`: both adapters rebuild, and managed labels
   are re-ensured for the (possibly new) team. `GET /api/v1/connections/registry`
   feeds the admin SPA at runtime. The SPA cold-start FALLBACK
   (`admin/spa/src/lib/registry_fallback.json`) is a **pinned mirror** of that
   payload (ADR-0034) — not a second authority; `test_spa_registry_fallback.py`
   fails on drift.

4. **`ForgeDescriptor` owns the dev-side dialect.** Everything forge-specific
   that is not an API call — PR/MR CLI instructions, clone auth user, git
   identity, CLI token env names, token secret shapes — ships as a classvar on
   the adapter. It reaches Dev containers via spec_env
   (`DEVCAKE_CLONE_USER/GIT_NAME/GIT_EMAIL/FORGE_CLI_ENVS`); the entrypoint's
   fallbacks reproduce v0 bit-for-bit, so app/image rollout order does not
   matter (`DEVCAKE_FORGE`/`DEVCAKE_FORGE_TOKEN` remain the legacy contract).
   The `devcake/{key}` branch convention is defined once:
   `ports/forge.py:mission_branch()`.
   *(Addendum: entrypoint fallbacks and `DEVCAKE_FORGE` discriminator removed
   at v0 crystallization — see Addendum below. Branch naming is now
   instance-prefixed via `mission_branch(instance, key)`.)*

5. ~~**Config schema v2 — plural now, single-instance runtime.**~~
   **SUPERSEDED** by schema v4 / ADR-0009 / multi-PMO: `pmos:`/`repos:` are
   **0..N** name-keyed instances (not "exactly one"); migration machinery was
   removed at crystallization (stale schemas refused). Historical body kept for
   provenance: *`pmo:`/`repo:` blocks became `pmos:`/`repos:` lists with
   exactly one entry enforced; multi-instance is a future wiring change…*
   Runtime today is multi-instance; see supersession note at top.

6. **Registry-driven redaction.** `security.py` = static platform lists +
   contributions from **every registered** adapter (configured or not — no gap
   when switching). A superset tripwire test pins the v0 lists as literals; it
   was written before the rewrite and must never be weakened.

7. ~~**Doc fiction removed.**~~ **PARTIALLY SUPERSEDED:**
   `ForgeCapabilities` **exists** today as a ClassVar on each forge adapter;
   `cancel_mission` **is** on `PMOPort` and used by decomposition/sweeps.
   Historical body: *Never-implemented interfaces (`watch()`/`ChangeEvent`,
   `cancel_mission()`, `MissionDraft`, `ensure_pr()`,
   `authenticated_clone_url()`, `RepoRef`, `ForgeCapabilities`) are deleted
   from docs/05–06…* — treat the struck list as the 2026-07-13 snapshot, not
   current law.

## Intentional behavior deltas — the complete ledger

The refactor preserves v0 behavior **except** these four (anything else that
differs is a regression):

| # | Delta | Why |
|---|---|---|
| a | PMO test endpoint counts managed labels as the `ALL_LABELS` intersection (was `startswith("DEVCAKE")`), and reports `labels_expected` | The old count inflated on custom DEVCAKE-prefixed labels; the probe now answers "is the managed set bootstrapped?" |
| b | Config PUT hot-reloads the PMO adapter (key/team changes apply immediately; labels re-ensured) | Previously a silent restart-required trap |
| c | GitLab API origin derives from the repo URL, `api_base` overrides (GitHub Enterprise likewise) | Self-hosted instances were unreachable; gitlab.com behavior identical |
| d | `/health` and test endpoints probe via public `health_probe()` (response keys preserved) | Removes private `_team()` reach-ins and raw vendor JSON from the API layer |

## Consequences

- Adding a PMO system = one adapter package implementing `PMOPort` + one
  `PMO_SYSTEMS` entry (+ contract tests). Adding a forge = adapter +
  `descriptor` + registry entry (+ CLI baked into dev images if its dialect
  needs one). Neither touches the domain, prompts, entrypoint, redaction, or
  the SPA.
- The port contracts are pinned by tests: `test_pmo_contract.py` (surface,
  signatures, fake drift, unified dispatch) and `test_forge.py` (conformance,
  DTO shape parity across adapters, descriptor completeness).
- ~~Multi-instance runtime remains future work…~~ **SUPERSEDED** — multi-PMO /
  multi-repo runtime ships (ADR-0009, schema v4); config and Run records carry
  `pmo_ref`/`repo_ref`.

## Follow-up — run-infrastructure ports + RunBootstrap (2026-07-14)

Second-tier seams deferred in decision §1 are now formalized (docs/01 §3,
docs/04 §3.1). This does **not** reopen vendor pluggability decisions above.

| Port / module | Role | Production adapter |
|---|---|---|
| `ExecutorPort` | start/stop/status Dev runs; `ExecutorError` / `DuplicateRun` | `adapters/dagu` |
| `StatePort` | run-record persistence | `adapters/files` (`run_store`) |
| `MessagingPort` | Redis Streams ACL + ingress/reply; `MessagingError` | `adapters/redis` |
| `RunFinalizer` | mission finalize / steward finalize / INV-3 restore | `MissionManager` / `FinalizerRouter` |
| `ReceiptStore` | harness bake receipts (staffing fail-closed) | `adapters/files/receipts` |
| `HarnessVersionSource` | operator-asked remote CLI latest | `adapters/registry_versions` |
| `ClaimsNotebooks` | memory notebook `.claims/` write | `adapters/claims_writer` |
| `OidcTokenPort` | control-plane OAuth token refresh (host-side) | `adapters/xai` |
| `domain/run_bootstrap.py` | deep dispatch spine shared by hello, mission, steward, OAuth | — |

`RunManager` binds the finalizer via `set_finalizer` after composition (breaks
the construct-time `RunManager` ↔ `MissionManager` cycle). Tests pin the
spine and finalizer routing at `tests/test_run_bootstrap.py`. Wire adapters
must not leak httpx/redis types upward (executor + messaging error contracts
parallel forge F7). SQLite (or other) `StatePort` swaps remain backlog
(`16-roadmap.md`) — the Protocol is the stable seam.

## Addendum — v0 crystallization (2026-07-13)

Two compatibility surfaces this ADR deliberately kept were removed at the v0
crystallization (founder decision: app and Dev images always deploy in
lockstep — `13-deployment.md` §8; no cross-version shims):

- **Decision 4's entrypoint fallbacks are gone.** `forge_dialect()` now
  *requires* the descriptor vars and crashes loudly when one is missing; the
  `DEVCAKE_FORGE` legacy discriminator is no longer shipped in spec_env
  (`DEVCAKE_FORGE_TOKEN`, the credential, remains).
- **Decision 5's migration machinery is gone.** `migrate_config` (v1→v2
  on-load, `.v1.bak`) and `migrate_config_patch` were removed; a v1
  `config.yaml` is refused at boot with hand-migration instructions, and
  v1-shaped PUT bodies get a 422 (`reject_v1_patch`) — still never silently
  dropped. Pre-v2 run records quarantine at boot (`10-persistence.md` §5).

The behavior-delta ledger above is unchanged — those four deltas shipped with
the refactor and stand.
