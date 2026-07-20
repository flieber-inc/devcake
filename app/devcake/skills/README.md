# DevCake skill store

Agent skills for DevCake Devs. Skills selected on a Dev Type (admin panel →
Config → Dev Types) are installed inside the Dev container before the agent
starts, into the harness's skills directory (`harness.py` `skills_dir`:
claude-code → `~/.claude/skills/`; grok-build and codex → `~/.agents/skills/`).
All three harness CLIs consume the same `SKILL.md` format.

## Philosophy (normative)

**Binding decision record:** [`docs/adr/0016-skills-and-prompt-assembly.md`](../../../docs/adr/0016-skills-and-prompt-assembly.md)
(ADR-0016). This README is the operator/author contract that implements it.

Skills are **domain modules**, not mission scripts. DevCake already composes
work from three layers — keep them separate:

| Layer | Owns | Does not own |
|---|---|---|
| **Mission playbook** | Step contract: workspace, legal outcomes, branch/PR, step rubrics | Company knowledge; long craft methods |
| **Identifying prompt** | Vehicle persona under a workflow (Development / Customer Success / …) | Step outcomes; full domain manuals |
| **Skills** | Optional domain knowledge packages | Mission contracts; competing copies of playbook/persona rules |

**Rules for every skill (built-in or operator-authored):**

1. **Domain-only** — encode a durable domain (engineering craft, dependency
   reasoning, a customer's product model, …). Never encode Mission Type
   machinery (`ONBOARD` outcomes, stage labels, branch naming, “when you are
   in EXECUTE…”).
2. **Additive** — each skill owns one unique slice. Skills must not overlap
   iron laws or contradict each other. Any subset must remain coherent;
   no skill may assume another is loaded. Companion routing (“also see X”)
   is fine; duplicating X is not.
3. **Consult-optional by default** — installing a skill makes it **Available**:
   the harness discovers it (name + description); the model may load the body
   when the description matches. DevCake must run correctly with **zero**
   skills selected.
4. **Soft-force Required (optional)** — marking a skill Required on a Dev Type
   also appends a short “you must consult these skills” block to the run
   prompt. That is **instructional soft-force**, not kernel enforcement.
   Harnesses do not guarantee skill load. UI and docs say so honestly.
5. **Self-contained** — a skill must work when consulted alone. Excellent
   `description` frontmatter is the primary activation path for Available
   skills. Do not write “you were required to load me” into the body.
6. **Never replace prompts** — if step behavior is missing, fix the playbook;
   if persona is weak, fix the identifying prompt. Do not skill-wrap the
   product.

One skill may attach to **many** Mission Types and Dev Types (e.g. a company
readme skill on every vehicle; dependency reasoning on judgment and mapper).

## Available vs Required

| Mode | Meaning | Mechanism |
|---|---|---|
| Off | Not in the container | Not in `DevType.skills` |
| **Available** | Installed; Dev **may** consult | Install into skills dir |
| **Required** | Installed **and** instructed to consult | `DevType.skills_required` ⊂ `skills` + prompt append |

Default seed packs leave both lists **empty** — operators choose chips.

## Layout

One directory per skill; the directory name is the skill name:

```
<skill-name>/SKILL.md      # required
<skill-name>/<anything>    # optional supporting files, loaded on demand
```

`SKILL.md` starts with YAML frontmatter:

```yaml
---
name: <skill-name>            # must equal the directory name
description: >                # what it does AND when to use it —
  ...                         # this is the model's activation trigger
metadata:
  source: original (devcake)  # or upstream URL if vendored
  author: devcake
---
```

Skill names: lowercase alphanumerics plus `-`/`_`, max 64 chars
(`^[a-z0-9][a-z0-9_-]{0,63}$`). Keep bodies focused (< ~500 lines); the
name + description load into every session, the body only on activation.

### Writing checklist

- [ ] Domain, not mission step
- [ ] Unique ownership vs other skills in the store
- [ ] Imperative voice; anti-patterns where useful
- [ ] No interactive “ask the user for a sprint backlog” loops — Devs read
      the activity feed and repo
- [ ] No restatement of legal outcomes / result.json / branch conventions
- [ ] Excellent description (activation)
- [ ] Original DevCake skills: `metadata.source: original (devcake)` and
      **no** `license:` field (the project has none yet)
- [ ] Vendored adaptations: keep upstream `license` and attribution footer

## Built-in catalog

| Skill | Unique domain |
|---|---|
| `systematic-debugging` | Root cause before fix |
| `test-driven-development` | Red→green at public seams |
| `verification-before-completion` | Evidence before “done” claims |
| `pr-hygiene` | Commit and PR as units of review |
| `dependency-reasoning` | When work A blocks work B |

## Adding / editing skills

Easiest: the DevCake admin panel → Config → **Skills** → **Add skill**
(write a name + description + instructions, or import a skill folder) — no
Gitea login and no YAML required.

Or push straight to `main` of this repo (the Gitea UI works). Either way
changes take effect on the next dispatched run — no restart needed. The
admin panel's Skills section lists what the store currently serves.

Attach skills on **Dev Types** as Available or Required (click-cycle chips).

## Re-seeding

DevCake re-seeds MISSING files from its bundled copies at every boot (and
via the admin panel's "Restore built-in skills" button): your edits to
existing files are never overwritten, but a deleted built-in file returns
on the next boot. To retire a built-in skill, simply don't select it on any
Dev Type. Upgrades that remove a name from the image stop re-seeding that
name; operator copies in the store are left alone.

## Attribution

Skills vendored or adapted from other projects keep their upstream
`license` frontmatter and attribution footer — do not strip these; they
are the terms under which the content may be redistributed. **Original
DevCake skills carry no license** (the project has none yet) and use
`metadata.source: original (devcake)`.
