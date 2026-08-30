# Host refresh — the pre-v1 wipe-and-re-onboard ritual

> **Audience:** the operator moving a live DevCake host onto a newer `main`.
> **Doctrine:** below v1 the project carries no backwards-compat obligation
> (`docs/16` versioning doctrine) — when persisted-state migrations are the
> risk, wiping `/data` and re-onboarding is a legitimate, supported path.
> This page is that path. Migrating in place remains possible; it is simply
> not what this ritual does.

The order matters: **receipts first** (they decay), **backups second**
(they include secrets), **wipe third**, and the wipe touches **one volume
only**.

## Phase 0 — export receipts (they decay; do this first)

Two clocks erase evidence on a live host: OpenObserve retention ages spans
out, and any Clear-runs sweep deletes run records and `activity-*` repos.

```bash
python3 scripts/export_receipts.py --days 90
```

The pack (deploy identity, raw trace spans as JSONL, `/data` minus
secrets) is private material — mission content, no credentials. Log
streams are skipped but named in `MANIFEST.json`; add `--include-logs`
if you want them. Repeat per host.

## Phase 1 — full backups (password-export tier)

1. **Settings bundle** — admin UI → Settings → Profiles & Export → export the encrypted
   settings bundle (ADR-0013). This is the re-onboard accelerator: config,
   profiles, and secrets in one file.
2. `scripts/backup_data.sh` — the `/data` volume, secrets included.
3. `scripts/backup_gitea.sh` — the internal Gitea volume (repos, boards,
   `activity-*`, sqlite DB).

Both tarballs hold plaintext credentials: store them like a
password-manager export. With no output path, both scripts write under
`${XDG_DATA_HOME:-$HOME/.local/share}/devcake/backups` (not into the
checkout). Prefer a quiet snapshot: `docker compose stop app` (and
`gitea` for the gitea pair) before each backup, then `up -d` after —
live snapshots are crash-consistent only (`13` §8).

## Phase 2 — the wipe scope (one volume, not four)

| Volume / path | Action | Why |
|---|---|---|
| `devcake_devcake_data` (`/data`) | **Wipe** | The only home of persisted app state — run store, config, `steward.yaml`, secrets. Wiping it is what makes the upgrade migration-free. |
| `devcake_gitea_data` | **Keep** | Boards (Gitea Issues), internal repos, `activity-*` knowledge trail. No app state lives here; on a forge-issue host this volume *is* the board. |
| OpenObserve volume | Optional | Exhaust, not knowledge ([ADR-0033](../adr/0033-discovery-routing-the-counterflow-lane.md) Decision 8: PMO = knowledge ledger, OpenObserve = operations exhaust; wiping OO must not change mission behavior). After Phase 0 it is fully disposable; keeping it costs only disk. |
| `devcake_mirrors`, workspaces dir | Ignore | Disposable caches; the stack rebuilds them. |

```bash
docker compose down
docker volume rm devcake_devcake_data
```

`.env` stays: stack bootstrap secrets (admin/Redis/Dagu/OO/Gitea
passwords, `DOCKER_GID`) live there, not in `/data`.

## Phase 3 — redeploy from the target commit

The deploy ritual from `docs/13` (deploy ordering — the DAG bind-mount
goes live at `git pull`, so dagu must stop first):

```bash
docker compose stop dagu    # belt-and-braces; up.sh re-asserts it
git pull                    # or: git checkout <pinned commit>
./up.sh --bake              # bakes images + compose up, tag-lockstep
```

Record the deployed commit (`git describe --tags --always`) — field
evidence is only citable when pinned to the deploy that ran it.

## Phase 4 — re-onboard

1. Admin UI → import the **settings bundle** from Phase 1. Import applies
   the current schema to older bundles (suite-covered); the fully-clean
   alternative is hand-reconfiguring connections and secrets through the
   admin UI from your records.
2. Verify every connection: PMO and forge test buttons, `secrets-check`
   presence ticks, per-instance health in `/health`.
3. Review staffing and toggles against current defaults — seeds only
   apply to fresh configs, so anything you re-imported keeps its old
   values by design.

## Phase 5 — first-poll smoke (before trusting it)

- [ ] `/health` green; no unexpected warnings beyond your known posture.
- [ ] Poll now → the boards' missions appear with correct provenance.
- [ ] One trivial mission end-to-end (opt-in label on a sandbox ticket, or
      the internal-forge path) reaches Done with transcript + token report
      on the feed.
- [ ] OO dashboard receives fresh spans (collector path alive).
- [ ] Config UI serves the current knobs (a quick way to catch a stale
      image: a feature merged on `main` that the UI does not show means
      the bake or tag-lockstep failed).

## Phase 6 — the observation window opens

The refresh is when the field evaluation starts, not ends. For roughly a
week, note: freshness re-review trips and exhaustions, incidents of
lost upstream context, and cross-subtree "I wish that mission had known
this" moments — the last class is exactly what ADR-0033's discovery
routing exists to serve, and these observations tune its constants.
