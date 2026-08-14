# ADR-0020 — Per-repository merge doctrine

- **Status:** accepted (2026-07-29)
- **Context:** `auto_merge`, `auto_resolve_merge_conflicts`, and
  `merge_retry_window_minutes` lived as deployment-global fields on
  `AppConfig` and as a master switch at the bottom of the Repos page. Every
  REVIEW finalize and merge sweep read `mgr.config.*`, so one repo could not
  auto-merge while another parked at `DEVCAKE-MERGE`. ADR-0019 had listed
  `auto_merge` as a possible per-PMO-instance candidate; merge doctrine is
  really a property of the forge repo (tokens, branch protection, CI weight),
  not of the PMO board.

> **Amended by ADR-0035 (2026-08-14):** at the same chokepoint, a merge
> target that is memory-bound anywhere (any `memory_repos` listing, or
> the sole work repo of a Curator board) additionally requires the
> global `memory_auto_merge` (default OFF — a person merges every
> note). Card-level `auto_merge` alone is no longer sufficient there.

## Decision

### 1 — Three fields move onto `RepoInstance` as one package

| Field | Default | Notes |
|---|---|---|
| `auto_merge` | `false` | App squash-merges after REVIEW approve on **this** repo |
| `auto_resolve_merge_conflicts` | `true` | Inert while `auto_merge` off; conflict → EXECUTE rework (max 2) |
| `merge_retry_window_minutes` | `30` (`ge=0`) | Inert while `auto_merge` off; deferred-merge sweep window |

Runtime reads via the mission's resolved repo:
`mgr.forges.instance(run.repo_ref)` / `mgr.forges.instance(m.repo)` (always
co-populated with `forges.get` by `rebuild` / `register_internal`).

Re-arm on config PUT is per-repo: only repos that flipped OFF→ON join
`mgr.rearm_merge_repos`; the next sweep reopens parked `DEVCAKE-MERGE`
missions whose `m.repo` is in that set. A repo absent from the previous
config counts as OFF, so a card removed and re-added with `auto_merge: true`
re-arms that repo's parked missions — deliberate: its merge queue resumes
visibly (feed marker) instead of waiting for a human forever.

### 2 — Internal (zero-repo) repos always auto-merge

Zero-repo missions get a synthesized `RepoInstance` at provision time
(`dispatch.resolve_repo_live`). No human watches the internal Gitea, and the
deliverable zip only posts after merge — parking there is a dead end. The
synthesized instance always sets `auto_merge=True`,
`auto_resolve_merge_conflicts=True`, `merge_retry_window_minutes=30`.

**Escape hatch:** an operator who wants doctrine control creates the repo as
a config card via "gitea (internal) → + Create repository" — cards get the
normal per-repo toggles.

### 3 — Pre-v1: no migration

Unknown top-level keys (including the old global merge fields) are dropped by
pydantic; per-repo defaults apply. `load_config` logs a WARNING listing
dropped top-level keys **and** a second WARNING when any of the three former
doctrine keys appear — a prior global `auto_merge: true` becomes per-repo
`false` until re-enabled on cards. Schema version stays **4**. No
`_stale_shape_reason` extension. The warning is boot-only: a PUT body
carrying the legacy top-level keys drops them silently (the SPA ships with
the app, so a stale client is not a supported case pre-v1).

Config PUT **and** profile/bundle apply both re-arm parked `DEVCAKE-MERGE`
missions for repos that flipped OFF→ON (`auto_merge_flipped_on` +
`rearm_merge_repos`).

### 4 — Out of scope

`attach_merged_changeset_to_pmo`, `adoption_mode`, and concurrency remain
deployment-global.

## Consequences

- Admin SPA: merge toggles live **inside each repo card** on `#/repos`;
  Save-dialog labels use `cfg.repos.N.auto_merge` paths.
- Docs (`02`, `03`, `04`, `06`, `10`, `11`, …) rephrase to "when the mission's
  repo has `auto_merge` on."
- ADR-0019 / docs/16 no longer list `auto_merge` as a per-PMO candidate.
