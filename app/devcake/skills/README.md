# DevCake skill store

Claude Code skills for DevCake Devs. Skills selected on a Dev Type (admin
panel → Config → Dev Types) are installed to `~/.claude/skills/` inside the
Dev container before the agent starts — claude-code harness only.

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
license: MIT | Apache-2.0     # this skill's license
metadata:
  source: <upstream URL or "original (devcake)">
  author: <author/org>
---
```

Skill names: lowercase alphanumerics plus `-`/`_`, max 64 chars
(`^[a-z0-9][a-z0-9_-]{0,63}$`). Keep bodies focused (< ~500 lines); the
name + description load into every session, the body only on activation.

## Adding / editing skills

Easiest: the DevCake admin panel → Config → **Skills** → **Add skill**
(write a name + description + instructions, or upload skill files) — no
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

## Licensing

Each skill carries its own `license` frontmatter and attribution in
`metadata`/its footer. Vendored skills keep their upstream license (MIT /
Apache-2.0); check the footer before redistributing modified copies.
