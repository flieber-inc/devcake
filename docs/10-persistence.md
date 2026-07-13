# 10 — Persistence: The `/data` Volume

> **Audience:** implementers and operators.
> **Decision record:** `adr/0002-file-based-persistence.md` (files over a database), `adr/0003-pmo-as-single-source-of-truth.md`.

All local app data lives as plain files on one named volume (`devcake_data`, mounted at `/data`) so it is trivially inspectable, diffable, and recoverable. **Backup story: back up `/data`; everything else is reconstructible.**

## 1. Layout (normative)

```
/data/
  config/
    config.yaml                 # human-editable general config (§3)
    dev_types/{name}.yaml       # one file per Dev Type (admin-panel CRUD target)
  secrets/
    {dev_type}/creds.json       # uploaded credential JSONs; chmod 600, owner app
  state/
    runs/{run_id}.json          # Run records (02-domain-model.md §7), one file per run
    events.jsonl                # append-only audit log of every PMO write the app performs
  cache/
    …                           # last-poll snapshot etc.; rebuildable; deletable at any time
```

## 2. Format rules

- **YAML** for anything humans edit (`config/`); **JSON** for machine state (`state/runs/`); **JSONL** for the append-only audit log.
- Every file carries `schema_version`. The pydantic models (`app/devcake/config.py` for config, `app/devcake/domain/run.py` for run records) are the single schema definition for all of them (`02-domain-model.md`).
- **Atomic writes, always:** write to `{path}.tmp` in the same directory → `fsync` → `rename`. JSONL appends are line-buffered with `fsync` per finalization.
- **Migrations:** applied on load, before validation, forward-only (`migrate_config` in `config.py`); the atomic-write discipline above is preserved — the migrated file is persisted with the same tmp→fsync→rename path (ADR-0002). Purely **additive** fields with defaults need no version bump: the Run record gained `pmo_ref`/`repo_ref` (both default `"main"`) with no schema change — pre-existing run JSONs parse unchanged.

## 3. `config.yaml` — annotated example (normative shape)

Schema **v2**: the connection blocks are plural lists (`pmos:`/`repos:`) with an `id` per entry — the forward-compatible shape for multi-PMO/multi-repo — but **exactly one entry each is enforced in v0** (`02-domain-model.md` §9).

```yaml
schema_version: 2

pmos:                                # exactly one entry in v0 (validated); id is what
- id: main                           #   Run records reference as pmo_ref
  system: linear                     # must be registered in the adapter registry (05 §1a)
  api_key_env: LINEAR_API_KEY        # name of the env var holding the key (app env / .env)
  team_key: ENG                      # the single team DevCake watches — nothing outside it
  api_base: null                     # null = the adapter's default API host

repos:                               # exactly one entry in v0 (validated); id → repo_ref
- id: main
  forge: github                      # must be a registered forge (github | gitlab)
  url: https://github.com/acme/product
  api_base: null                     # null = api.github.com / the repo's origin
  default_branch: main
  token_env: GITHUB_TOKEN
  reviewer_token_env: null           # optional 2nd credential for formal PR approvals (06 §4)

assignments:                         # every Mission Type must be assigned to exactly one Dev Type.
  ONBOARD:                           #   extra_cli_args are appended verbatim to the harness invocation —
    dev_type: senior-dev             #   admin-set data, harness-specific, NEVER hardcoded (02 §9).
    extra_cli_args: "--max-turns 15" # seeded default: bounded-effort triage for claude-code; edit/clear freely
  PLAN:
    dev_type: senior-dev
    extra_cli_args: ""
  EXECUTE:
    dev_type: main-dev
    extra_cli_args: ""
  REVIEW:
    dev_type: senior-dev
    extra_cli_args: ""

concurrency:
  global_max: 3                      # ceiling across ALL Devs; per-type caps live in dev_types/*.yaml

adoption_mode: opt_in                # opt_in (default): only missions labeled DEVCAKE are adopted;
                                     #   opt_out: the whole team, entire backlog included (02 §2)
poll_interval_seconds: 30
dev_timeout_minutes: 120             # enforced by the app watchdog (04 §5)
max_attempts: 3
review_loop_warning_every: 3
auto_merge: false                    # true = DevCake merges its own PRs, no human intervention
auto_resolve_merge_conflicts: true   # inert while auto_merge is off: conflicts → EXECUTE rework (max 2)
merge_retry_window_minutes: 30       # inert while auto_merge is off: sweep retries not-yet-mergeable PRs this long
intake_paused: false                 # operator switch: no NEW dispatches while true (11 §2)
relations_mapper:                    # ADR-0007: manual-only by default; periodic service is opt-in
  enabled: false
  interval_minutes: 60
  dev_type: junior-dev
dismissed_alerts: []                 # admin-UI state: dismissed advisory alerts ("id:signature")
```

**v1 → v2 migration (on load, forward-only):** a `config.yaml` with `schema_version: 1` (or none) — singular `pmo:`/`repo:` blocks — is upgraded in memory before validation (`pmo:` → `pmos: [{id: main, …}]`, likewise `repo:`) and persisted atomically; the original file is first copied to **`config.yaml.v1.bak`**, which is the rollback story. The migration is idempotent and there is no down-migrator.

`dev_types/{name}.yaml` mirrors the DevType fields of `02-domain-model.md` §6 exactly.

## 4. Write path and hot reload

The admin panel writes config **through the app's API** (`PUT /api/v1/config`): validation happens once, in the app's pydantic models. PUT semantics:

- The body is a **partial patch**, deep-merged over the current config (`deep_merge`): nested dicts merge recursively, so `{"concurrency": {"global_max": 5}}` never resets sibling fields — but **non-dict values are replaced wholesale**. In particular `pmos`, `repos`, and `dismissed_alerts` are lists: a PUT that touches them must send the **whole replacement list**.
- **Legacy singular bodies are adapted, not dropped:** `migrate_config_patch` translates a v1-shaped body (`{"pmo": {…}}` / `{"repo": {…}}`) into the plural v2 shape by merging over the current single entry. Load-bearing, not defensive — pydantic ignores unknown keys, so without it a stale client's PUT would silently lose the operator's edit instead of failing.
- On success the app writes atomically and calls `reload_connections()`: the PMO and forge adapters are **rebuilt immediately** from the saved config and the managed labels are re-ensured (`05-pmo-adapter.md` §1a) — connection changes no longer wait for a restart. The remaining fields hot-apply at the next poll cycle.

Direct file edits are tolerated but take effect on the next app start, when `load_config` re-validates (and, if needed, migrates) the file.

## 5. What deleting things costs (INV-1 restated)

| Deleted | Consequence |
|---|---|
| `/data/cache` | Nothing — rebuilt next poll. |
| `/data/state` | Run history, attempt counters, and loop-warning dedupe reset. Mission state is untouched (it lives in the PMO); reconciliation (`04-orchestrator.md` §6) rebuilds the in-flight picture from the Dagu API and Redis. Legal at any time. |
| `/data/secrets` | Dev Types with `credentials_json` mode fail auth (exit 12 → circuit breaker) until re-uploaded. |
| `/data/config` | The app blocks startup pending reconfiguration (admin panel first-run flow). |
