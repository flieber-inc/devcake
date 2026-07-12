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
- Every file carries `schema_version`. Pydantic models in `app/models.py` are the single schema definition for all of them (`02-domain-model.md`).
- **Atomic writes, always:** write to `{path}.tmp` in the same directory → `fsync` → `rename`. JSONL appends are line-buffered with `fsync` per finalization.
- **Migrations:** at startup, in-place migrators upgrade any file whose `schema_version` is behind, oldest first; unknown (newer) versions block startup with a clear error.

## 3. `config.yaml` — annotated example (normative shape)

```yaml
schema_version: 1

pmo:
  system: linear
  api_key_env: LINEAR_API_KEY        # name of the env var holding the key (app env / .env)
  team_key: ENG                      # the single team DevCake watches — nothing outside it

adoption_mode: opt_in                # opt_in (default): only missions labeled DEVCAKE are adopted;
                                     #   opt_out: the whole team, entire backlog included (02 §2)

repo:
  forge: github                      # github | gitlab — one active repo per instance
  url: https://github.com/acme/product
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

dev_timeout_minutes: 120             # enforced by the app watchdog (04 §5)
poll_interval_seconds: 30
auto_merge: false                    # true = DevCake merges its own PRs, no human intervention
auto_resolve_merge_conflicts: true   # inert while auto_merge is off: conflicts → EXECUTE rework (max 2)
merge_retry_window_minutes: 30       # inert while auto_merge is off: sweep retries not-yet-mergeable PRs this long
review_loop_warning_every: 3
max_attempts: 3
```

`dev_types/{name}.yaml` mirrors the DevType fields of `02-domain-model.md` §6 exactly.

## 4. Write path and hot reload

The admin panel writes config **through the app's API** (`PUT /api/v1/…`): validation happens once, in the app's pydantic models; on success the app writes atomically and applies the change at the next poll cycle. Direct file edits are tolerated: a file watcher re-validates and reloads (invalid edits are rejected with the previous config kept live and a health warning raised).

## 5. What deleting things costs (INV-1 restated)

| Deleted | Consequence |
|---|---|
| `/data/cache` | Nothing — rebuilt next poll. |
| `/data/state` | Run history, attempt counters, and loop-warning dedupe reset. Mission state is untouched (it lives in the PMO); reconciliation (`04-orchestrator.md` §6) rebuilds the in-flight picture from the Dagu API and Redis. Legal at any time. |
| `/data/secrets` | Dev Types with `credentials_json` mode fail auth (exit 12 → circuit breaker) until re-uploaded. |
| `/data/config` | The app blocks startup pending reconfiguration (admin panel first-run flow). |
