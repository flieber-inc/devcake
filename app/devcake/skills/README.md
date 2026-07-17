# DevCake skill store

Agent skills for DevCake Devs. Skills selected on a Dev Type (admin panel →
Config → Dev Types) are installed inside the Dev container before the agent
starts, into the harness's skills directory (`harness.py` `skills_dir`:
claude-code → `~/.claude/skills/`; grok-build and codex → `~/.agents/skills/`).
All three harness CLIs consume the same `SKILL.md` format.

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
  source: <upstream URL or "original (devcake)">
  author: <author/org>
---
```

Vendored skills additionally carry the upstream project's `license`
frontmatter and attribution footer — keep those intact (see below).

Skill names: lowercase alphanumerics plus `-`/`_`, max 64 chars
(`^[a-z0-9][a-z0-9_-]{0,63}$`). Keep bodies focused (< ~500 lines); the
name + description load into every session, the body only on activation.

## Adding / editing skills

Easiest: the DevCake admin panel → Config → **Skills** → **Add skill**
(write a name + description + instructions, or import a skill folder) — no
Gitea login and no YAML required.

Or push straight to `main` of this repo (the Gitea UI works). Either way
changes take effect on the next dispatched run — no restart needed. The
admin panel's Skills section lists what the store currently serves.

## Re-seeding

DevCake re-seeds MISSING files from its bundled copies at every boot (and
via the admin panel's "Re-seed built-ins" button): your edits to existing
files are never overwritten, but a deleted built-in file returns on the
next boot. To retire a built-in skill, simply don't select it on any Dev
Type.

## Attribution

Skills vendored or adapted from other projects keep their upstream
`license` frontmatter and attribution footer — do not strip these; they
are the terms under which the content may be redistributed. DevCake's own
skills carry no license (the project has none yet).
