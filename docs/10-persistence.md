# 10 — Persistence: The `/data` Volume

> **Audience:** implementers and operators.
> **Decision record:** `adr/0002-file-based-persistence.md` (files over a database), `adr/0003-pmo-as-single-source-of-truth.md`.

All local app data lives as plain files on one named volume (`devcake_data`, mounted at `/data`) so it is trivially inspectable, diffable, and recoverable. **Backup story: back up `/data`; everything else is reconstructible.**

Run records are accessed through **`StatePort`** (`ports/state.py`); the production adapter is `adapters/files/run_store.py`. A future SQLite (or other) store is an adapter swap behind that port (`adr/0002`, `16-roadmap.md`) — not a domain change.

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
    runs/quarantine/            # unreadable/model-invalid/pre-v2 records, moved aside at boot (§5)
    events.jsonl                # append-only audit log of every PMO write the app performs
```

(The poll snapshot served by `GET /api/v1/missions` is in-memory only — rebuilt
every cycle, nothing on disk.)

## 2. Format rules

- **YAML** for anything humans edit (`config/`); **JSON** for machine state (`state/runs/`); **JSONL** for the append-only audit log.
- Every file carries `schema_version`. The pydantic models (`app/devcake/config.py` for config, `app/devcake/domain/run.py` for run records) are the single schema definition for all of them (`02-domain-model.md`).
- **Atomic writes, always:** write to `{path}.tmp` in the same directory → `fsync` → `rename`. JSONL appends are line-buffered with `fsync` per finalization.
- **Schema evolution:** purely **additive** fields with defaults need no version bump — the Run record gained `pmo_ref`/`repo_ref` (both default `"main"`) that way. There is no auto-migration machinery: the v1→v2 migrators were removed at v0 crystallization (founder decision); pre-v2 data is refused (config) or quarantined (run records) with instructions, never silently upgraded.

Run records are schema **v2**. They contain non-secret execution context and a one-way Redis envelope verifier, never raw Redis passwords, forge/model credentials, or credential-file content. Secret run-spec material is not persisted anywhere: the app builds it from current config when the Dev sends `runspec.get` (`09-messaging.md` §§3, 5). At every boot, an integrity sweep (`RunStore.quarantine_unreadable`) moves unparseable, model-invalid, or **pre-v2** records to `runs/quarantine/` (0600, named in the log) — so one corrupt record can never block boot, and a restored v1 backup (which persisted credentials) can never sit silently in the store. A record that still parses as JSON is **scrubbed of known credential-bearing fields before the move** (quarantine must not become secret-at-rest); only unparseable bytes are preserved verbatim, for inspection, under the restrictive modes. Because a quarantined record is forgotten, boot also best-effort tears down anything it may have left live — the Dagu run, the per-run Redis ACL user, the reply stream — keyed on the file's run id. Quarantined files are removed by clear-runs.

## 3. `config.yaml` — annotated example (normative shape)

Schema **v3** (docs/16 M9): the connection blocks are plural lists of **instances-with-identities** — each entry carries an operator-chosen `name` (lowercase alnum, ≤12 chars, no hyphens; the pattern is `^[a-z][a-z0-9]{0,11}$`). The name is the instance's identity everywhere: `Run.pmo_ref`/`repo_ref`, branch prefixes (`devcake/LINEAR-DEV-17`), run ids. N≥1 PMO instances are supported (unique names; two instances must not target the same `(system, api_base, team_key)`); repos stay exactly-one until M10. An instance with an empty `team_key` (or a repo with an empty `url`) is **valid but idle** — skipped by the poll loop and label bootstrap, shown as unconfigured in `/health`.

**Hand-migration from older schemas** (the app refuses stale files at boot; auto-migration was removed at v0 — there are no deployments): v1 → rename `pmo:`/`repo:` blocks to one-entry `pmos:`/`repos:` lists; v2 → rename each entry's `id:` to a chosen `name:` (e.g. `linear` / `main` — note the format rule above); then set `schema_version: 3`. Existing run records with `pmo_ref: main` stay valid: review/merge lookups fall back to the pre-v3 unprefixed branch convention (`ports/forge.py legacy_branch`), and the FinalizerRouter routes them to the sole manager. **Add a SECOND instance only after draining pre-v3 in-flight runs** — with ≥2 managers a legacy record's owner is ambiguous and the router fails it cleanly rather than guessing a workspace. Alternatively delete the file and reconfigure via the admin panel.

```yaml
schema_version: 3

pmos:                                # N≥1 instances; name is what Run records
- name: linear                       #   reference as pmo_ref, and the branch/run-id prefix (uppercased)
  system: linear                     # must be registered in the adapter registry (05 §1a)
  api_key_env: LINEAR_API_KEY        # name of the env var holding the key (app env / .env)
  team_key: ENG                      # the team this instance watches — nothing outside it
  api_base: null                     # null = the adapter's default API host
  default_repo: null                 # M10 routing: repo name missions default to (null = zero-repo gate)

repos:                               # exactly one entry until M10 (validated); name → repo_ref
- name: main
  forge: github                      # must be a registered forge (github | gitlab)
  url: https://github.com/acme/product
  api_base: null                     # null = the adapter's default API host / the repo's origin
  default_branch: main
  token_env: ""                      # empty = derived from the forge descriptor (GITHUB_TOKEN)
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

**v1 configs are refused, not migrated:** a `config.yaml` carrying the singular `pmo:`/`repo:` blocks fails startup with a clear error telling the operator to migrate by hand (`pmo:` → `pmos: [{id: main, …}]`, likewise `repo:`, `schema_version: 2`) or delete the file and reconfigure via the admin panel. Detection keys on the **singular keys, never on an absent `schema_version`** — an empty file or a hand-written v2 config without the version field boots normally (defaults apply). Refusing loudly beats validating silently: pydantic ignores unknown keys, so accepting v1 data would reset the operator's connections to defaults.

`dev_types/{name}.yaml` mirrors the DevType fields of `02-domain-model.md` §6 exactly.

## 4. Write path and hot reload

The admin panel writes config **through the app's API** (`PUT /api/v1/config`): validation happens once, in the app's pydantic models. PUT semantics:

- The body is a **partial patch**, deep-merged over the current config (`deep_merge`): nested dicts merge recursively, so `{"concurrency": {"global_max": 5}}` never resets sibling fields — but **non-dict values are replaced wholesale**. In particular `pmos`, `repos`, and `dismissed_alerts` are lists: a PUT that touches them must send the **whole replacement list**.
- **v1-shaped bodies are rejected, not dropped:** `reject_v1_patch` refuses `{"pmo": {…}}` / `{"repo": {…}}` with a 422 naming the plural v2 shape. Load-bearing, not defensive — pydantic ignores unknown keys, so without the guard a stale client's PUT would silently lose the operator's edit instead of failing.
- On success the app writes atomically and calls `reload_connections()`: the PMO and forge adapters are **rebuilt immediately** from the saved config and the managed labels are re-ensured (`05-pmo-adapter.md` §1a) — connection changes no longer wait for a restart. The remaining fields hot-apply at the next poll cycle.

Direct file edits are tolerated but take effect on the next app start, when `load_config` re-validates the file.

## 5. What deleting things costs (INV-1 restated)

| Deleted | Consequence |
|---|---|
| `/data/state` | Run history, attempt counters, and loop-warning dedupe reset. Mission state is untouched (it lives in the PMO); reconciliation (`04-orchestrator.md` §6) rebuilds the in-flight picture from the Dagu API and Redis. Legal at any time. |
| `/data/secrets` | Dev Types with `credentials_json` mode fail auth (exit 12 → circuit breaker) until re-uploaded. |
| `/data/config` | The app blocks startup pending reconfiguration (admin panel first-run flow). |
