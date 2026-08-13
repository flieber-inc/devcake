# ADR-0016 — Skills philosophy and prompt assembly layers

- **Status:** accepted (2026-07-20)
- **Context:** DevCake composes every Dev run from identifying prompts, mission
  playbooks, and optional skill-store packages. Early product work shipped
  **illustrative vendored skills** (generic PM ceremony, interactive craft
  guides) that blurred those layers: they assumed a chatty human, restated or
  competed with step contracts, and invited mission-specific “skill playbooks”
  (`onboard-mission`, …). Separately, skill selection was install-only with no
  product language for **Available vs Required**, so operators could not tell
  consult-optional from obligated. This ADR freezes the layering and skill
  content rules so playbooks, Dev Types, and the skill store stay complementary
  rather than three competing instruction systems.

## Decision 1 — three prompt layers, never collapsed

Every mission (and mapper) run’s **composed prompt** is:

```text
spec_prompt =
    identifying prompt          # Dev Type vehicle × active workflow preset
  + mission-type playbook       # (or MAPPER playbook)
  + [optional] required-skills soft-force block
```

Skill **files** are not inlined into that string. They are materialized into the
harness skills directory (`DevType.skills` → runspec `skills` / `skills_dir`)
before harness launch (`08-harness-templates.md` §7a).

| Layer | Owns | Must not own |
|---|---|---|
| **Mission playbook** (per Mission Type, workflow-templated) | Step contract: workspace paths, legal outcomes / `result.json`, branch/PR rules, step rubrics | Company product encyclopedias; long craft methods; skill iron laws |
| **Identifying prompt** (per Dev Type, workflow-templated: Development / Customer Success / …) | Vehicle persona and quality bar under that workflow | Step outcomes; full domain manuals; competing copies of the playbook |
| **Skills** (selected on the Dev Type) | Optional **domain** knowledge modules | Mission Type machinery; persona redefinition; legal outcomes |

**Invariant — zero-skills:** DevCake must complete any step correctly with
**empty** `DevType.skills` and `skills_required`. Skills enrich; they never
substitute playbooks or identifying prompts. If step behavior is missing, fix
the playbook. If persona is weak, fix the identifying prompt.

Workflow presets (e.g. Development vs Customer Success) may reframe **both**
identifying prompts and playbooks. Machinery (outcome shapes, branch/PR rules)
stays identical across workflows; only framing and quality language change
(`prompts/customer_success.py`).

## Decision 2 — skills are domain-specific, never mission-specific

A skill encodes a **durable domain** (engineering craft, dependency reasoning,
a customer’s product model, CS methodology, …). It may be attached to **any**
number of Mission Types and Dev Types.

A skill **must not**:

- Encode Mission Type contracts (`ONBOARD` / `PLAN` / `EXECUTE` / `REVIEW` /
  `MAPPER` outcomes, stage labels, branch naming, “when you are in EXECUTE…”).
- Invent or restate `LEGAL_OUTCOMES` / `result.json` legality.
- Assume an interactive operator (“ask for sprint capacity”); Devs read the
  activity feed and repo under the playbook.

Mission-step specialist skills (`onboard-mission`, `execute-mission`, …) are
**rejected**. Cross-step utility is expected (e.g. company orientation on every
vehicle; dependency reasoning on `judgment` **and** `mapper`).

Authoritative authoring contract for store content: `app/devcake/skills/README.md`.
Domain model fields: `docs/02-domain-model.md` §6 (`skills`, `skills_required`).

## Decision 3 — skills are additive

The ideal roster is orthogonal modules: the operator can toggle chips and know
each chip adds **one unique information package**.

| Rule | Meaning |
|---|---|
| **Unique ownership** | Each skill owns one domain slice (one iron law or procedure family) |
| **Non-contradiction** | Skills never give opposite rules for the same decision |
| **No soft re-implementation of prompts** | Skills do not drift-prone copies of playbook or persona text |
| **Safe under partial selection** | Any subset remains coherent; no skill requires another to be loaded |
| **Companion routing only** | “Also see skill X” is allowed; duplicating X’s rules is not |

Built-in skills default to **empty** selection on seeded Dev Types — operators
choose packs. Suggested combinations live in docs/tutorials, not forced defaults.

## Decision 4 — consult-optional by default; Required is soft-force only

| Mode | Config | Runtime meaning |
|---|---|---|
| **Off** | not in `skills` | Not installed in the container |
| **Available** | in `skills` | Installed; harness discovers name + description; model **may** load the body (description match). Default when selected. |
| **Required** | in `skills` **and** `skills_required` | Installed **and** the composed prompt appends a short “must consult these skills” block listing names that actually shipped in the runspec payload |

Invariants:

- `skills_required` is a **subset** of `skills` (validated on `DevType`).
- Missing or size-capped skills are skipped with warnings at payload assembly;
  they do **not** appear in the Required block.
- Required is **instructional soft-force**, not kernel enforcement. Harnesses
  do not guarantee skill load. Admin UI and docs state this honestly
  (`docs/11-admin-panel.md`, `docs/08` §7a).
- Skill bodies must not claim “you were required to load me”; that language
  lives only in the prompt-layer append (`dispatch.append_required_skills`).

Admin control: tri-state chips (off → Available → Required → off) on the Dev
Type card.

## Decision 5 — Dev Types are vehicles, not skill containers or seniority ranks

Seeded Dev Types name **roles of the vehicle** (harness, model, concurrency,
skill chips), not junior/main/senior theater and not one-to-one skill packs:

| Seed name | Default mission assignment (v0) |
|---|---|
| `judgment` | ONBOARD, PLAN, REVIEW |
| `implementer` | EXECUTE |
| `mapper` | Relations Mapper default |

Assignments (`AppConfig.assignments`; labeled **Mission Types** in the UI
since the 2026-08-02 nav reorg — the config field stays `assignments`) remain
the Mission Type → Dev Type map.
Skills attach freely across vehicles. Review ≠ Execute as separate *vehicles*
remains a security **recommendation** (`docs/14`), not forced by skill design.

Identifying prompts stay **short personas** (“do exactly what the current
mission playbook asks”). They do not absorb playbook contracts or skill
manuals.

## Decision 6 — skill store delivery and trust class (unchanged posture)

- Skills live in the skill-store repo (bundled Gitea) with **bundled image
  fallback** when the forge is down (`domain/skills.py`).
- Boot / “Restore built-ins” seeds **missing paths only** — never overwrites
  operator edits.
- Built-in names cannot be deleted via the admin API (re-seed); retirement is
  deselect on Dev Types. Operator and retired-but-still-in-store names remain
  deletable.
- Trust class: operator-controlled agent instructions, same class as MCP setup
  commands (`docs/14-security.md`). Path confinement guards placement, not
  content.

## Rejected alternatives

- **Mission-step skills as the primary specialization mechanism** — duplicates
  playbooks, breaks multi-step attachment, and invites outcome drift.
- **Inlining skill bodies into `spec_prompt`** — blows context, breaks harness
  skill discovery, and forces every skill into every turn.
- **Hard kernel enforcement of skill reads** — no portable guarantee across
  claude-code / grok-build / codex; false security. Soft-force + honesty only.
- **Skills required for correct product operation** — violates zero-skills and
  couples dispatch to store availability.
- **Seniority-named default Dev Types** — poorly maps to assignments, model
  cost, and skill packing; replaced by role vehicles (`judgment` /
  `implementer` / `mapper`).
- **Built-in interactive PM ceremony skills** (sprint planning, dual PRDs,
  status reports, …) as the default catalog — wrong unit of work for unattended
  Devs; operators may still import such skills if their board needs them.

## Consequences

- Normative field semantics for `DevType.skills` / `skills_required` and the
  Required prompt append are binding for implementers and docs (02, 03 §7, 07,
  08 §7a, 11).
- Content policy for built-in and operator skills is binding for the store
  README and admin authoring UX.
- Workflow presets remain the axis for Development vs Customer Success (and
  future board kinds); skills are not a second workflow system.
- Future skill features (e.g. per-mission skill overrides) must preserve
  domain-only content, additivity, zero-skills, and soft-force honesty — or
  amend this ADR.

## Addendum — external skill repos (2026-08-13, founder rulings)

The ops-quartet campaign adds a THIRD skill source: **external git
repositories, as configured repo cards** — the founder's ruling collapsed an
earlier dedicated-machinery design onto existing pipes ("they should
essentially work just like a forge adapter — and could just be one").

1. **Source = a `RepoInstance` card.** A Dev Type selects an external skill
   as `<card>/<skill>` (one slash; prefix in card shape — `SKILL_NAME_RE`
   stays slash-free, so the store/builtin and external namespaces are
   STRUCTURALLY disjoint: no precedence rules exist anywhere). The card's
   optional `skills_subdir` names where `<skill>/SKILL.md` dirs live.
   Credentials, URL validation, branch (`default_branch`, moving — founder
   ruling), health, alerts and the SPA card UI are all inherited. Private
   repos work day one via the card's tokens (token_ro preferred).
2. **Cache = the ADR-0024 mirror, read-side.** No second cache, no
   checkout, no new volume: `RepoCache` gains `tree_head` /
   `read_skill_tree` / `read_skill_file` (git plumbing over the bare
   mirror; reads pin a sha first, so a concurrent fetch cannot tear them).
   Supply-chain guards live at that seam: only blob modes 100644/100755
   (symlink entries skipped), only dir names matching the skill regex, and
   the payload caps run against `ls-tree -l` sizes BEFORE content reads.
   `adapters/git.GitResult` gains `raw_stdout` (verbatim bytes) so file
   content never rides the lossy text decode.
3. **Fail-closed sync (founder ruling), as a set-union in the EXISTING
   ADR-0024 gate.** Skill-source cards join `needed_for`'s result before
   the ONE `ensure_fresh` call (`repo_sourcing.skill_source_cards` — a
   deliberately separate function: skill cards are NEVER part of
   `sourced_repo_names`, they feed the runspec payload and are not cloned
   into /workspace). An unconfigured prefix or internal-forge card fails
   the sync with a named reason and defers dispatch. The same union guards
   the three StewardService gates. **Scoping of Decision 3:** the
   fail-closed boundary is the SYNC GATE; payload READS stay additive — a
   read failure after a passed gate warns and drops the skill, never
   refuses the run. Zero-skills is intact (no external names ⇒ the union
   is empty ⇒ byte-identical behavior).
4. **Payload flattening keeps the container contract untouched.** An
   external skill `myrepo/tdd` ships payload paths under the BASENAME dir
   (`tdd/…`), so `install_skills` and every harness's discovery rule work
   unchanged — `images/common/*` is deliberately untouched. A per-Dev-Type
   basename-uniqueness validator refuses selections that would fight over
   one install dir. The Required-skills prompt append names the basename
   with its origin.
5. **Provenance:** `Run.skill_repo_heads` records {card: sha} consumed at
   dispatch (docs/14 trust-class: the operator owns WHICH repo and its
   credentials; upstream owns the moving content; the app records the
   consumed commit per run).
6. **Read-only by construction:** authoring names refuse `/` (save/delete),
   the admin catalog tags external rows with their origin and disables
   Delete; `GET /skills/{name}` accepts the path form. `/skills/sync` also
   `ensure_fresh`es referenced cards — one refresh gesture.

## Related

- `app/devcake/skills/README.md` — operator/author contract
- `docs/02-domain-model.md` §6 DevType · `docs/03-mission-lifecycle.md` §7 prompts
- `docs/08-harness-templates.md` §7a · `docs/11-admin-panel.md` Skills / Dev Types
- `docs/14-security.md` skill-store trust class
- Implementation: `config.DevType`, `domain/orchestrator/dispatch.append_required_skills`,
  `domain/skills.SkillService`, admin `SkillModeChips` / Skills View
