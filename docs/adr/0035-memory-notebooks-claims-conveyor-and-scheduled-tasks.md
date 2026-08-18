# ADR-0035 — Memory-compatible notebooks, the `.claims/` conveyor, and Scheduled Tasks

**Status:** Accepted (2026-08-14); amends ADR-0020 (memory merge guard) and the
ADR-0016 addendum (shared `context_sourcing_strict` knob). Decided across
three Fable↔Grok debate rounds plus founder rulings (D1–D12, F1′–F4 — the
`devcake-internal/memorytalk` repo holds the full record); built by
PR #150 + remediation. **Not "shipped"**: docs/16 still gates that claim on
pre-registered throwaway-box A/B pilot receipts (historically tracked as
`PLAN_MEMORY.md` §14–§15). Distilled decisions live **in this ADR**; the
former root `PLAN_MEMORY.md` build plan is **not in the repository** (deleted
after the decisions were accepted here) and must not be treated as a live
openable contract.

## Context

DevCake is deliberately memoryless between runs: ADR-0033's discoveries
are per-mission surplus learning, not a cross-mission store. The founder
wanted long-lived, cross-board team memory WITHOUT DevCake becoming a
memory product — and without marrying any memory philosophy (what a note
should say, how a notebook should be organized, when to write one). The
resulting doctrine: **memory-compatible, not memory-product** — DevCake is
opinionated about logistics (mounting, queueing, gating, scheduling) and
silent about memory science. "The notebook describes itself": layout
authority is the notebook's own README, operator-owned, never enforced or
seeded by the app.

## Decisions

1. **A notebook is an ordinary repository card plus bindings — no
   `kind: memory`.** `pmos[].memory_repos` (board-bound) and
   `DevType.memory_repos` (domain-bound); usage chips are derived. Two
   invariants, validated cross-store: **I1** — within one instance,
   `repos` / `reference_repos` / `memory_repos` are pairwise disjoint;
   **I2** — a memory-bound card is never one work repo among others: for
   every instance either `m ∉ repos` or `repos == [m]` (the latter is a
   **Curator board**). Instance-side names are existence-checked like the
   other lists.

2. **Consumer mounts.** Runs whose instance/Dev-Type union binds
   notebooks clone them read-only at `/workspace/memory/<card>/` (card
   name, sibling of `repo/`), on every stage **including STEWARD**
   (reads because it works discoveries; still never authors — ADR-0033
   D7). The union always excludes the run's primary `repo_ref`. Mounts
   are snapshotted on the Run (card/binding/commit/stale/strict) — a
   binding added mid-flight never appears. When any mount exists, the
   prompt gains one fixed factual sentence naming `/workspace/memory/`
   and `.claims/` — no coaching beyond that.

3. **One failure knob for context: `context_sourcing_strict`
   (default true).** Amends the ADR-0016 addendum: skill sources and
   memory notebooks share it. Strict = a run whose context cannot be
   fetched does not start — deferred at dispatch (provisioning family,
   no attempt, never `DEV_BAD_OUTPUT`), and a strict memory mount's
   in-container clone failure is fatal for the provision step (exit-13
   forge family, same as the primary clone). Open = last-good mirror
   with a `stale_cache` marker, or omit-and-continue. Work / reference /
   blocker extras keep their existing rules.

4. **The fast lane is the `.claims/` conveyor — doubt at machine speed.**
   At discovery harvest (after feed memorialization, never gating it)
   the app copies each entry to every notebook on the run's dispatch
   snapshot as `.claims/<id>.json` — one file per lead, id =
   hash(source_instance, source_pmo_id, step, index), dedup =
   file-exists. Append creates files, drain deletes files, Clear-prune
   deletes files: the three writers structurally cannot merge-conflict.
   `.claims/README.md` (create-if-missing, never rewritten) carries the
   leads-not-truths framing. Caps: `budgets.claims_queue_max` per
   notebook, refuse-new-never-evict. Conveyor failure never fails the
   discovering run. The writer is forge-neutral (clone+commit+push with
   the card's write token, one push-race retry) — never a Dev token.
   The app reads/writes ONLY under `.claims/`; note bodies stay
   app-blind (wipe test: delete every note, replay the same Dev
   outputs, the mission loop is unchanged).

5. **The slow lane is an ordinary pull request — truth at human speed.**
   Notes are written only by runs whose primary work repo IS the
   notebook (a Curator board), through the normal
   EXECUTE→REVIEW→merge pipeline. **Amends ADR-0020's merge
   chokepoint:** a memory-bound target additionally requires
   `memory_auto_merge` (default OFF ⇒ a person merges every note; ON is
   an explicit consent mode whose UI copy says what it is — two models
   in a row, not a person, not the reviewer token). App commits under
   `.claims/` do not pass this gate.

6. **Scheduling is a generic Cron module with ONE verb** — create a
   labeled ticket from a template on a timer (`AppConfig.crons`;
   `DEVCAKE` + stage label, `devcake:cron:v1 job=<id>` marker,
   single-flight per board, intake-pause honored, elapsed-interval
   schedule). Outcomes ride a file ledger
   (`state/cron_outcomes.json`): degradation (3 failed automatic fires)
   is restart-safe and pauses only the schedule — Run-now always works
   and a success re-arms. The reserved, non-deletable **memory-curator**
   row fans out one EXECUTE ticket per Curator board per fire; automatic
   fires skip a notebook whose `.claims/` is empty (confirmed by
   listing), Run-now never skips. A bound notebook with no Curator board
   is a standing health warning, never a silent skip. Curator runs
   inherit their consumers' `repos ∪ reference_repos` as ordinary
   non-fatal read-only extras. No shipped Curator Dev Type, playbook, or
   layout. In the UI this is **Configuration → Scheduled Tasks**: the
   built-in DevCake tasks (Relations Steward — moved from Traffic
   control — and Memory Curator) over the operator's Custom tasks; the
   word "Cron" never reaches the operator.

7. **The Relations Steward's instruction half became operator-editable**
   (`steward.playbook_template`, `{mission_table}` substitution) for
   parallelism with the Curator's ticket text — supersedes the
   2026-07-14 "STEWARD stays un-templated" ruling for the relations
   flavor only; the result-contract half and the discovery flavor stay
   code-owned.

8. **Notebooks survive Clear** (ADR-0014 operator-repo class; the
   auto-provision path refuses `activity-*` names). Clear-all prunes
   every `.claims/*.json` (all origin boards are being wiped; orphans
   from deleted boards have no owner left) and leaves every note.

## Consequences

- Memory quality is entirely the operator's: DevCake guarantees
  delivery, gating, and provenance — never that a note is true or that
  a Dev reads it. The pre-registered A/B pilot (historically
  `PLAN_MEMORY.md` §15; criteria now owned by docs/16 residuals) must be
  allowed to report "nobody consulted the notebook"; claims-seen and
  claims-drained are reported even if they embarrass the feature.
- Chosen risk, on the record: the full loop (mounts, conveyor, cron,
  curation) was built before measuring consultation. The pilot's
  falsification power is narrower for it.
- N parallel curation pipelines share `global_max`; per-fire listing
  costs one shallow notebook checkout. Operator cost, not kernel
  complexity.
- Explicit non-goals (do not relitigate in PRs): `CONTESTED.json`,
  promote-to-memory routing judgment, harness-native memory dirs,
  app reads of note bodies, subfolder bindings, claim eviction,
  reply-reopened curation missions.

## References

Historical build plan `PLAN_MEMORY.md` (retired / not in-tree; §17 rulings
log lived there during design); ADR-0014 (operator repos), ADR-0016 addendum
(strict knob lineage), ADR-0020 (merge chokepoint, amended here),
ADR-0024/0025 (mirror + provision), ADR-0033 (discoveries; D7 steward
non-authorship); docs/02/03/07/09/10/11/14/16 carry the seam-level detail
and the pilot/"shipped" residual gate.
