# ADR-0034 — Chokepoints: one authoritative path per singular process

- **Status:** accepted (2026-08-12) — founder ruling on the 2026-08-12
  seven-reviewer audit at `ccc6da9`; the audit's cross-cutting finding was
  that the codebase's guarantees are held by discipline (comments, naming
  conventions, hand-mirrored copies) rather than structure, and that
  singular processes had grown parallel implementations that drifted.
  Implementation = the intervention campaign's Phase 1 (chokepoint PRs) +
  Phase 0's validation unification, each PR citing this ADR.
- **Extends:** ADR-0028 (the structure-guard ratchet pattern in
  `test_structure_guards.py` becomes this ADR's enforcement arm), ADR-0013
  (its "same choke points as the config PUT" promise becomes doctrine),
  ADR-0027 (taxonomy-as-data is the same move at data level).

## Context — discipline is not structure

The audit could not falsify "engineered with care": checkpoint idempotency,
TOCTOU guards, fail-closed defaults, and injection defenses all held under
hostile line-by-line reading. What it *could* falsify is "well-factored."
The failure pattern was uniform across subsystems:

1. **A singular process implemented in several places.** "Mission completes
   on merge" existed at four call sites; the AUD-010 tristate conflict rule
   at two (and they had already split doctrine once); config validation ran
   as two paths — one strict for bundles, one blind for the PUT — that had
   measurably drifted; the CI bring-up existed twice (`up.sh` vs
   `ci_compose_for_dispatch.sh`); the Dev bus chunking twice (`bus.py` vs
   `hello_dev.py`, already drifted); the repo-sourcing rule three times.
2. **The copies kept "in sync" by comment.** `RepoCache.needed_for` carries
   "must mirror `_extra_repos_for`'s sourcing exactly"; `_SWAP_MARKER_STAGE`
   carries "MUST be registered here"; the SPA hand-mirrors the board derive
   table, the instance-name regex, and scaffold defaults with a "Mirrors X"
   comment and no cross-language test.
3. **First implementations rigorous, second copies without the guards.**
   The Linear adapter refuses truncated label rewrites; its gitea_issues
   twin silently destroyed labels past the page boundary. The port declared
   "adapters must never leak httpx exceptions" in bold; all three forge
   adapters leaked them while both PMO adapters wrapped correctly.

Fix-commit recidivism confirms where this bites: the files whose invariants
live in prose (`api/main.py`, `domain/runs.py`, `config.py`) are exactly
the repeated fix-commit hotspots. The system was safe because one person
held its conventions in their head — the maintenance risk concentrated
precisely where the conventions lived as comments.

## Decision

**A singular process has exactly one implementation.** Anything that looks
like a second implementation must be one of:

- a **call** into the one authoritative function/module (the chokepoint);
- a **derived view** computed from the one authoritative registry — never a
  hand-maintained parallel table;
- a **pinned mirror**: where a copy is physically unavoidable (another
  language, another runtime, a container payload), a test loads both sides
  from ONE fixture or compares them field-by-field, so drift turns red.

Corollaries, each load-bearing:

1. **Guards travel with the chokepoint, not the caller.** A rule like
   "never rewrite from a truncated read" or "wrap transport exceptions"
   lives inside the shared function so the next tenant inherits it by
   construction, not by imitation.
2. **Registries over string literals.** When control flow branches on a
   family of magic strings (checkpoint step keys), the family gets one
   registry carrying the strings AND their attributes; consumers derive.
   The strings' byte values are frozen by canary tests — a registry is a
   refactor of ownership, never of wire/record compatibility.
3. **Enforcement is a ratchet test, not a review habit.** Every chokepoint
   this ADR creates ships with a structure guard in the
   `test_structure_guards.py` style (AST scan / import ban / fixture
   comparison) whose failure message names the chokepoint to use. Guards
   are anti-accident, not anti-adversary, and say so in their docstrings.
4. **Comments that say "keep in sync with X" are defects.** Each one is
   either replaced by a call/derivation/pinned mirror, or documents WHY the
   sync cannot be structural — dated, with the guard that watches it.

## The inventory this campaign closes

| Singular process | Was | Becomes |
| --- | --- | --- |
| Mission completion on merge | 4 sites (review.py `_done`/`_done_merged`, sweeps merged branch, deferred-retry tail) | `orchestrator/completion.complete_merged` — one `_CAUSES` table owns copy/labels/audit per cause |
| AUD-010 conflict trust + routing | 2 copies + cross-module `_` reach-in | `completion.trusted_conflict` / `route_conflict_to_execute`; import-identity ratchet |
| Checkpoint step keys | ~40 literals; 2 hand-copied side registries (`_SWAP_MARKER_STAGE`, `_PAST_GATE_STEPS`) | `orchestrator/steps.py` registry; side tables become derived views; AST guard on `_checkpoint`/`finalized_steps.append` |
| Repo sourcing (mission-type → repo set) | 3 copies (`needed_for`, `_extra_repos_for`, `_blocker_mount_ok`) kept in sync by comment | one shared predicate (the `at_decomposition_limit` precedent); gate-set == mount-set parametrized test. PLAN_MEMORY: `sourced_repo_names` also includes consumer memory mounts and Curator inherit extras (`repos ∪ reference_repos` of consumer boards); `_extra_repos_for` skips memory names (they clone to `/workspace/memory/<card>/`) |
| Config/assignments validation | 2 paths (PUT blind, bundle strict) + endpoint-only completeness | `config.validate_assignment_map` at the model + unconditional cross-store semantics (shipped, Phase 0 PR-C) |
| Secret-apply ordering | promise in a docstring, code did the opposite | planner + ADD/MUT phases around one commit point (shipped, Phase 0 PR-C) |
| Adapter transport guards | per-adapter folklore (3 forge `_req`s leak httpx; download loop duplicated and drifted) | `adapters/http.forge_request` + `adapters/_toolkit` (download, REST pagination, asset-ref recovery); parametrized contract tests |
| Truncated-rewrite refusal | Linear only; gitea_issues destroyed data | guard inside the shared pagination/refusal path (shipped for gitea_issues, Phase 0 PR-B) |
| Backup/restore container payloads | inline `sh -c` strings per script | `scripts/lib/*_payload.sh`, shared with the pytest restore drill (shipped, Phase 0 PR-A) |
| SPA ↔ backend contracts | 3 hand-mirrors ("Mirrors derive()…", instance-name regex, scaffold defaults) | one generated `contracts.json` pinned by BOTH pytest and the SPA suites |
| CI bring-up env derivation | `up.sh` and `ci_compose_for_dispatch.sh` derive independently | one sourced helper; CI script keeps only CI-specific concerns |
| Dev bus chunking | `hello_dev.py` hand-copy (already drifted: `SHRINKABLE_FIELDS`) | shared import or field-by-field pinned mirror |
| Layering rule (domain never imports adapters) | prose + review culture; 2 sanctioned seams undocumented/half-documented | import-ban ratchet with a 2-entry allowlist naming both seams |
| Vendor-cap comment pagination | split in `feed.py`; join/coalesce reimplemented in `activity_payload` (`isalnum` glue) | `feed.split_vendor_comments` / `feed.join_vendor_comments` / `feed.coalesced_step_files`; `activity_payload` only calls; AST ratchet |
| Forge-issue cancel footer | three `replace("---")` copies that deleted every horizontal rule | `adapters/forge_issue.apply_cancel_footer` / `strip_cancel_footer`; mapping modules re-export one `CANCEL_FOOTER` |
| Managed-label case fold | `ensure_labels` matched upper, `swap_labels`/`Mission.labels` used stored case | `model.canonicalize_labels` next to `ALL_LABELS`; three forge-issue adapters call it |
| Oldest-first comment ceiling | `paginate_rest` kept the oldest pages on GitHub/Gitea | `_toolkit.paginate_rest_newest` + `last_page_from_headers`; GitLab already sorts desc |

Out of scope, deliberately: unifying the checkpoint-vs-sweep idempotency
regimes (different failure economics, would change PMO read costs);
Linear's GraphQL connection walks joining `paginate_rest` (different shape
— forcing them together would manufacture a false abstraction, the inverse
error); `mgr`'s `_audit/_feed/_checkpoint` seam (ADR-0015-sanctioned — the
manager IS the chokepoint there).

## Consequences

- New code review question, now answerable mechanically: "which chokepoint
  does this go through?" — and if none fits, the PR either creates one or
  documents why the process is genuinely plural.
- The guards make the doctrine self-enforcing for accidents; deliberate
  evasion remains possible and remains a review matter.
- Cost accepted: chokepoints add one indirection at call sites, and
  registries front-load naming decisions. The audit's evidence is that the
  alternative cost — four-site doctrine splits, silent label destruction,
  a validation path that lies — compounds faster.
