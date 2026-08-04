# 10 — Persistence: The `/data` Volume

> **Audience:** implementers and operators.
> **Decision record:** `adr/0002-file-based-persistence.md` (files over a database), `adr/0003-pmo-as-single-source-of-truth.md`.

All local app **state** lives as plain files on the `devcake_data` volume (mounted at `/data`) so it is trivially inspectable, diffable, and recoverable. Since ADR-0024/0025 the app also holds two **non-state** stores this document is not the authority on: the `/mirrors` volume (bare source mirrors — a DISPOSABLE cache that re-warms) and the `$DEVCAKE_WS_HOST` host bind (`/workspaces` in-container — per-run scratch, reclaimed at run end). **Backup story: back up `/data` (via `scripts/backup_data.sh`) AND `gitea_data`; mirrors and workspaces are reconstructible and deliberately excluded** — the full story, including why the excluded trees still deserve host-snapshot care (they hold repo source), is `13-deployment.md` §8.

Run records are accessed through **`StatePort`** (`ports/state.py`); the production adapter is `adapters/files/run_store.py`. A future SQLite (or other) store is an adapter swap behind that port (`adr/0002`, `16-roadmap.md`) — not a domain change.

## 1. Layout (normative)

```
/data/
  config/
    config.yaml                 # human-editable general config (§3)
    dev_types/{name}.yaml       # one file per Dev Type (admin-panel CRUD target)
    prompt_templates/{TYPE}/{name}.yaml         # per-Mission-Type prompt templates (built-ins re-seeded at boot)
    devtype_prompt_templates/{dev}/{name}.yaml  # per-Dev-Type identifying-prompt templates
    profiles/{name}.yaml        # config profiles: section A of a settings bundle + metadata (ADR-0013)
  secrets/
    connections/{scope}-{instance}.json  # GUI-stored PMO/repo secret VALUES (ADR-0011); 0600
                                         #   pmo-board.json is APP-MINTED (ADR-0030: the default
                                         #   board's PAT) and self-healing — re-minted at boot/reload
                                         #   when revoked or lost; never operator-entered
    harness/{VAR}.json          # GUI-stored harness/model keys; 0600
    internal_forge/*.json       # bundled-Gitea service/mission tokens (ADR-0010); 0600
    profiles/{name}.json        # a profile's secret snapshot (section B); 0600, covered by the redaction glob
    {dev_type}/                 # uploaded credential JSONs (harness-specific
                                #   filenames, e.g. grok-auth.json, codex-auth.json
                                #   — not always creds.json); chmod 600, owner app
  state/
    runs/{run_id}.json          # Run records (02-domain-model.md §7), one file per run
    runs/quarantine/            # unreadable/model-invalid/pre-v2 records, moved aside at boot (§5)
    runlogs/{run_id}.log        # condensed Dev stdout (run.log relay; SSE for admin terminal)
    events.jsonl                # append-only audit log: every PMO write + settings changes (profile ops, exports)
    mission_owner.json          # multi-PMO claim map (which instance owns which pmo_id)
    profiles.json               # last-applied-profile breadcrumb (advisory; wiped harmlessly by clear-runs)
```

(The poll snapshot served by `GET /api/v1/missions` is in-memory only — rebuilt
every cycle, nothing on disk.)

## 2. Format rules

- **YAML** for anything humans edit (`config/`); **JSON** for machine state (`state/runs/`); **JSONL** for the append-only audit log.
- Versioned config and run records carry `schema_version` (config v4, runs v2). The pydantic models (`app/devcake/config.py` for config, `app/devcake/domain/run.py` for run records) are the single schema definition for those shapes (`02-domain-model.md`). Sidecars like `events.jsonl` and `mission_owner.json` are unversioned append/map files.
- **Atomic writes, always (config / run JSON / secrets):** write to `{path}.tmp` in the same directory → `fsync` → `rename`. **`events.jsonl` is append-only best-effort** — each audit line is written with a plain append; there is no per-line `fsync`.
- **Schema evolution:** purely **additive** fields with defaults need no version bump — the Run record gained `pmo_ref`/`repo_ref` (both default `"main"`) that way. There is no auto-migration machinery: the v1→v2 migrators were removed at v0 crystallization (founder decision); pre-v2 data is refused (config) or quarantined (run records) with instructions, never silently upgraded.

Run records are schema **v2**. They contain non-secret execution context and a one-way Redis envelope verifier, never raw Redis passwords, forge/model credentials, or credential-file content. `RunStore.all()` serves a **per-process mtime-keyed parse cache** (2026-08-02, chosen over SQLite/Redis): the app is one process, `save()` lands via `os.replace` (new inode, fresh mtime), so an unchanged `(mtime_ns, size)` pair proves an unchanged file — files stay the only truth, and the runs API's filter/sort/group work never re-parses an unchanged record. Secret run-spec material is not persisted anywhere: the app builds it from current config when the Dev sends `runspec.get` (`09-messaging.md` §§3, 5). At every boot, an integrity sweep (`RunStore.quarantine_unreadable`) moves unparseable, model-invalid, or **pre-v2** records to `runs/quarantine/` (0600, named in the log) — so one corrupt record can never block boot, and a restored v1 backup (which persisted credentials) can never sit silently in the store. A record that still parses as JSON is **scrubbed of known credential-bearing fields before the move** (quarantine must not become secret-at-rest); only unparseable bytes are preserved verbatim, for inspection, under the restrictive modes. Because a quarantined record is forgotten, boot also best-effort tears down anything it may have left live — the Dagu run, the per-run Redis ACL user, the reply stream — keyed on the file's run id. Quarantined files are removed by clear-runs.

### Wipe generation (`store_gen`)

Clear-runs must not be undone by in-flight writers that still hold a `Run` in memory (finalize, heartbeat, kill). The production `RunStore` keeps a process-local monotonic **`wipe_generation`** (starts at 0; not persisted across app restarts).

| Event | Behavior |
|---|---|
| `RunBootstrap.launch` | Stamps `run.store_gen = store.wipe_generation` before the durable save |
| `RunStore.clear()` | Bumps `wipe_generation`, then unlinks every run JSON (and quarantine) |
| `RunStore.save(run)` | **No-op** when `run.store_gen < wipe_generation` (log at info) |
| Mission / hello finalize | Early-abort when pre-wipe — no further PMO posts after a wipe is observed mid-flight |
| Startup `reconcile_runs` | Re-stamps adopted / finalizing-for-reclaim runs to **this** process's `wipe_generation` (resets to 0 on restart). Without that, a prior process's `store_gen ≥ 1` would survive a restart and miss the first post-restart clear |

Legacy records without the field load as `store_gen: 0` (additive optional field — no schema bump). After a clear in-process, only runs launched **after** the wipe (stamped with the new generation) may persist again.

## 3. `config.yaml` — annotated example (normative shape)

Schema **v4** (docs/16 M12): the connection blocks are plural lists of **instances-with-identities** — each entry carries an operator-chosen `name` (lowercase alnum, ≤12 chars, no hyphens; `^[a-z][a-z0-9]{0,11}$`). The name is the instance's identity everywhere: `Run.pmo_ref`/`repo_ref`, branch prefixes (`devcake/LINEAR-DEV-17`), run ids. **0..N** PMO instances and repos (empty = idle first boot). Two PMO instances must not target the same `(system, api_base, team_key)`; two repos must not target the same URL. An instance with an empty `team_key` (or a repo with an empty `url`) is **valid but idle**. **No `*_env` fields:** secret VALUES are GUI-stored under `/data/secrets/` (ADR-0011) — `config.yaml` holds no credentials.

**Hand-migration** (the app refuses stale files at boot; no deployments exist): v1 → plural lists; v2 → `id:`→`name:`; **v3 → v4: remove every `*_env` field** (`api_key_env`, `token_env`, `token_ro_env`, `reviewer_token_env`) and re-enter the secret VALUES via the Config page (or move them into `/data/secrets/connections/{scope}-{name}.json`); set `schema_version: 4`. Existing run records with `pmo_ref: main` stay valid (review/merge lookups fall back to the pre-v3 unprefixed branch via `legacy_branch`; the FinalizerRouter routes them to the sole manager). **Add a SECOND instance only after draining pre-v3 in-flight runs.** Alternatively delete the file and reconfigure via the admin panel.

```yaml
schema_version: 4

pmos:                                # 0..N instances; name is what Run records
- name: linear                       #   reference as pmo_ref, and the branch/run-id prefix (uppercased)
  system: linear                     # must be registered in the adapter registry (05 §1a)
  team_key: ENG                      # the team this instance watches — nothing outside it
  api_base: null                     # null = the adapter's default API host
  repos: []                          # the instance's ORDERED repo set (item 2, 2026-07-15):
                                     # first = default for unmarked missions; markers must
                                     # name a listed repo; [] = per-mission internal repos
  reference_repos: []                # read-only consultation clones for EVERY stage
                                     # (disjoint from repos; never work targets)
  assignments: {}                    # per-instance Mission-Type override rows (ADR-0019):
                                     # a present key replaces the GLOBAL assignments row below
                                     # WHOLESALE (dev_type + extra_cli_args together — args are
                                     # harness-specific, never mixed across rows); absent = inherit
                                     # live. e.g.  EXECUTE: {dev_type: csagent, extra_cli_args: ""}
                                     # the API key VALUE is GUI-stored: /data/secrets/connections/pmo-linear.json

repos:                               # 0..N (empty = every mission routes to the internal fallback forge)
- name: main
  forge: github                      # must be a registered forge (github | gitlab | gitea)
  url: https://github.com/acme/product
  api_base: null                     # null = the adapter's default API host / the repo's origin
  default_branch: main
  auto_merge: false                  # per-repo (ADR-0020): true = app merges after REVIEW; false = park
  auto_resolve_merge_conflicts: true # inert while auto_merge off: conflicts → EXECUTE rework (max 2)
  merge_retry_window_minutes: 30     # inert while auto_merge off: deferred-merge sweep window
                                     # token VALUES (token/token_ro/reviewer_token) are GUI-stored:
                                     # /data/secrets/connections/repo-main.json
                                     # internal (zero-repo) synthesized instances always auto_merge=true

assignments:                         # every Mission Type must be assigned to exactly one Dev Type.
  ONBOARD:                           #   extra_cli_args are appended verbatim to the harness invocation —
    dev_type: judgment               #   admin-set data, harness-specific, NEVER hardcoded (02 §9).
    extra_cli_args: "--max-turns 15" # seeded default: bounded-effort triage for claude-code; edit/clear freely
                                     #   --max-turns is claude-code + grok-build only; codex 0.146.0 has NO
                                     #   turn cap, so no args value bounds a codex Dev (08 §1, 15 §2a).
  PLAN:
    dev_type: judgment
    extra_cli_args: ""
  EXECUTE:
    dev_type: implementer
    extra_cli_args: ""
  REVIEW:
    dev_type: judgment
    extra_cli_args: ""

concurrency:
  global_max: 3                      # ceiling across ALL Devs; per-type caps live in dev_types/*.yaml

adoption_mode: opt_in                # opt_in (default): only missions labeled DEVCAKE are adopted;
                                     #   opt_out: the whole team, entire backlog included (02 §2)
poll_interval_seconds: 30
dev_timeout_minutes: 120             # enforced by the app watchdog (04 §5)
max_attempts: 3
recover_misplaced_result: true       # ADR-0018: accept a stray result file written during the run
continuation_policy: auto            # ADR-0022: auto | resume-only | fresh-only | off (07 §5a)
max_continuations: 2                 # ADR-0022: nudge relaunches per run; 0 = off; no upper bound
repo_mirror:                         # ADR-0024: source-mirror knobs (the mirror itself has no off switch)
  sync_max_age_seconds: 0            #   0 = sync before every dispatch (fail-closed gate, 07 §7b)
  lfs: false                         #   true = mirrors also carry LFS content
review_loop_warning_every: 3
attach_merged_changeset_to_pmo: false  # true = also zip PR files to PMO for configured repos (internal always zips)
intake_paused: false                 # master switch: no NEW dispatches on any PMO while true (11 §2)
# each pmos[] entry may also carry intake_paused: true  # per-instance freeze under the master
max_decomposition_depth: 2           # 0 = unlimited; ADR-0012 / 03 §1.3
relations_mapper:                    # ADR-0007: manual-only by default; periodic service is opt-in
  enabled: false
  interval_minutes: 60
  dev_type: mapper
active_prompt_templates: {}          # per-Mission-Type template name; missing ⇒ "default"
active_devtype_prompts: {}           # per-Dev-Type identifying-prompt name; missing ⇒ "Development"
dismissed_alerts: []                 # admin-UI state: dismissed advisory alerts ("id:signature")
```

**Stale configs are refused, not auto-migrated:** any `schema_version` other than **4**, or a body still carrying singular `pmo:`/`repo:` / `id:`-keyed / `*_env` shapes, fails startup with a clear error. Hand-migrate all the way to **schema v4 name-keyed** lists (`pmos: [{name: …}]`, `repos: [{name: …}]`, secret VALUES under `/data/secrets/connections/`, no `*_env` fields) **or** delete the file and reconfigure via the admin panel. There is no stepwise auto-upgrade through v2/v3.

**Pre-v1 field moves (ADR-0020):** merge doctrine used to be top-level (`auto_merge`, `auto_resolve_merge_conflicts`, `merge_retry_window_minutes`). Those keys now live under each `repos[]` entry. Pre-v1 policy: no migration — pydantic drops unknown top-level keys and per-repo defaults apply (`auto_merge: false`, …). **Operational footgun:** a file that still has top-level `auto_merge: true` will **stop** auto-merging after upgrade until each repo card is re-enabled. `load_config` logs a WARNING listing dropped keys and a second WARNING naming the legacy doctrine keys so this is not a quiet no-op.

`dev_types/{name}.yaml` mirrors the DevType fields of `02-domain-model.md` §6 exactly.

## 4. Write path and hot reload

The admin panel writes config **through the app's API** (`PUT /api/v1/config`): validation happens once, in the app's pydantic models. PUT semantics:

- The body is a **partial patch**, deep-merged over the current config (`deep_merge`): nested dicts merge recursively, so `{"concurrency": {"global_max": 5}}` never resets sibling fields — but **non-dict values are replaced wholesale**. In particular `pmos`, `repos`, and `dismissed_alerts` are lists: a PUT that touches them must send the **whole replacement list**.
- **Stale-shaped bodies are rejected, not dropped:** `reject_stale_patch` refuses singular `{"pmo": {…}}` / `{"repo": {…}}`, `id:`-keyed entries, `*_env` fields, and non-v4 `schema_version` with a 422 naming the current plural name-keyed shape. Load-bearing, not defensive — pydantic ignores unknown keys, so without the guard a stale client's PUT would silently lose the operator's edit instead of failing.
- On success the app writes atomically and calls `reload_connections()`: the PMO and forge adapters are **rebuilt immediately** from the saved config and the managed labels are re-ensured (`05-pmo-adapter.md` §1a) — connection changes no longer wait for a restart. The remaining fields hot-apply at the next poll cycle.

Direct file edits are tolerated but take effect on the next app start, when `load_config` re-validates the file.

## 5. What deleting things costs (INV-1 restated)

| Deleted | Consequence |
|---|---|
| `/data/state` | Run history, attempt counters, and loop-warning dedupe reset. Mission state is untouched (it lives in the PMO); reconciliation (`04-orchestrator.md` §6) rebuilds the in-flight picture from the Dagu API and Redis. Legal at any time. |
| `/data/secrets` | Dev Types whose harness credential files / harness secret VALUES lived here fail auth (exit 12 → circuit breaker) until re-uploaded or re-entered. GUI-stored connection secrets and profile secret snapshots are gone — re-enter via the Configuration page or re-import a bundle. |
| `/data/config` | The app blocks startup pending reconfiguration (admin panel first-run flow). |
| `/data/config/profiles` + `/data/secrets/profiles` | Saved profile snapshots are gone; **live settings are untouched** (profiles are fire-and-forget snapshots, ADR-0013). |
| the `/mirrors` volume (`docker volume rm devcake_mirrors`, stack stopped) | Nothing durable lost — the mirror is a mandatory but DISPOSABLE cache (ADR-0024): the next dispatch's fail-closed freshness gate re-clones every needed repo (one full re-fetch per repo of cost). Deleting it while the stack runs instead trips the volume error → dispatch refuses with a visible reason until `verify_writable` clears. |
| the `$DEVCAKE_WS_HOST` tree | Per-run scratch only (ADR-0025). In-flight runs' containers lose their bind and fail (counted per docs/15); terminal residue is exactly what the periodic sweep reclaims anyway. Never part of the backup set. |
