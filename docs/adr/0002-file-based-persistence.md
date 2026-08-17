# ADR-0002 — File-Based Local Persistence (No Database in v0)

**Status:** accepted (v0); **amended 2026-08-04** — the "backup = copy `/data`" sentence is superseded by `13-deployment.md` §8 (see amendment blockquote below).

## Context

The main app must store its local data "on files in a volume mount so they are easy to recover from failures" (mission doc). Because the PMO System is the single source of truth (ADR-0003), local state is limited to: config, credentials, run telemetry, an audit log, and rebuildable caches — a tiny write volume.

## Decision

Plain files on one `/data` volume: YAML for human-edited config, JSON-per-file for Run records, JSONL for the audit log; atomic tmp+fsync+rename writes; `schema_version` in every file; pydantic as the single schema authority. Layout in `10-persistence.md`.

## Alternatives considered

- **SQLite** — transactional and queryable, but adds migration tooling and opacity for a dataset that is never authoritative; files are `git diff`-able and debuggable over SSH.
- **Postgres container** — a sixth service and operational burden vastly out of proportion to the data.
- **Redis as state store** — conflates transport with storage and violates "Redis is never a source of truth" (`09-messaging.md`).

## Consequences

Backup = copy `/data`. Every consumer must use the atomic-write recipe. Escape hatch documented: if run history outgrows files, `state/runs/` swaps to SQLite behind `StatePort` without touching anything else (post-v0 backlog).

> **Amendment (2026-08-04):** "backup = copy `/data`" predates the app's two
> non-state stores — the `/mirrors` volume (ADR-0024, disposable cache) and
> the `$DEVCAKE_WS_HOST` workspace bind (ADR-0025, per-run scratch). The
> file-over-database decision stands unchanged; the backup sentence is
> superseded by `13-deployment.md` §8 (back up `/data` + `gitea_data`;
> mirrors/workspaces excluded by design), with `scripts/backup_data.sh` as
> the shipped vehicle.
