# ADR-0009 — One MissionManager per PMO instance (schema v3 multi-PMO)

- **Status:** accepted (2026-07-14, docs/16 M9)
- **Context:** F2 makes DevCake PMO-independent: one deployment oversees N≥1 PMO instances at once, with identities that keep branches, run ids, and ACL names collision-free. The entire orchestrator package was written against a single `self.pmo` and pmo_id-keyed advisory state.

## Decision

**One `MissionManager` per configured PMO instance**, iterated by the single poll loop, instead of threading an `instance` parameter through every orchestrator module.

- Per-mission logic is untouched: each manager owns `self.instance` (the `PMOInstance`), its bound adapter, and its advisory state (`_grace`, `anomalies`, `needs_human`, `merge_handoffs`, `blocked_reasons`, `cycles`, merge windows) — separation for free, no re-keying.
- **Shared state is shared by injection, never duplicated:** the `RunManager`/`RunStore` (global + per-dev-type concurrency caps see all instances' runs), ONE dev-type `breakers` dict (credentials are DevCake-global), and the forge runtime (single forge until M10, then the shared `ForgeRuntime`).
- **`FinalizerRouter`** (`domain/orchestrator/router.py`) is the `RunFinalizer` plugged into `RunManager`: it routes every callback on `run.pmo_ref`. A run whose instance vanished from config mid-flight fails cleanly with a persisted error — never crashes the ingress consumer. Legacy (pre-v3 `""`/`"main"`) records route to the sole manager only when exactly one exists.
- `build_managers()` reconciles the manager set **in place** on config PUTs: surviving instances keep advisory state with repointed adapters; removed instances drop theirs.
- **Cross-instance dedupe:** a Linear project can be accessible to two teams; the poll loop claims each raw `pmo_id` for the first configured instance and surfaces an anomaly on the others (`_claim_missions`) — double dispatch is structurally prevented, and cross-PMO `blocked_by` stays impossible by construction (each gate_map sees only its instance's missions).

## Identity conventions

- Instance names: `^[a-z][a-z0-9]{0,11}$` — no hyphens (the uppercased name prefixes `devcake/{INSTANCE}-{key}` branches and `{INSTANCE}-{key}-{seq}-{TYPE}-{sfx}` run ids; a hyphen would make the compounds ambiguous), ≤12 chars (64-char Dagu run-id budget).
- `Mission.instance` is stamped by the adapter at normalization (adapters are instance-bound), so no fetch path can return unstamped provenance; `mission_branch("")` fails loudly.
- HELLO/OAUTH runs use the fixed pseudo-instance `sys` and finalize inside `RunManager` — deliberately outside the router.
- Provenance rides on `Mission.instance` + `Run.pmo_ref`; `MissionRef` stays a 2-tuple (every consumer is an instance-bound adapter — a third field would be dead weight; devil's-advocate finding M3 in the v0.1 plan).

## Consequences

- `/health` reports per-instance PMO health (`pmo_instances`, grey when unconfigured) plus a scalar aggregate; advisory dicts merge with `[instance]` prefixes when N>1.
- An instance with an empty `team_key` is valid-but-idle (no manager, skipped by label bootstrap) — the defined state M12's GUI-only empty first boot inherits.
- Two instances must not target the same `(system, api_base, team_key)` (config-refused: double dispatch); the runtime dedupe covers the *shared project across distinct teams* hazard, which the validator cannot see.
