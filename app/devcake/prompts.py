"""Playbook prompts (docs/03 §7). The playbook restates the binding rules from
docs/03 — workspace boundaries, result.json contract, bounded effort. Prompts
inline only the mission title/description; activity/ is reference material."""

from .pmo import Mission

ONBOARD_PLAYBOOK = """
## Your current mission type: ONBOARD (triage)

Assess the complexity of the mission below against the actual codebase, then
route it. **Assess, don't deep-dive** — this is a bounded triage pass, not an
exploration. Do not modify any code in this mission type (the rare trivial
path is the only exception, and you must be certain).

### Workspace
- `/workspace/repo/` — a fresh clone of the repository. The ONLY place work may happen.
- `/workspace/activity/` — the mission's history and artifacts (ACTIVITY.md index +
  files). Reference material: grep or read what you need; do not assume you must
  read all of it.
- `/workspace/out/` — where your outputs go.

### The mission
- Key: {key}   ·   Priority: {priority}   ·   URL: {url}
- Title: **{title}**

{description}

### Classify (docs rubric)
- `trivial` — you are CERTAIN you can complete it now: localized (≤ ~2 files), zero
  design ambiguity, obvious verification. Rare.
- `normal` — a definable piece of work that needs a plan first. **Most missions.**
- `high` — too large/compound for one plan-execute-review cycle; splits naturally
  into independent work items. Rare.

### Required output — /workspace/out/result.json
Write EXACTLY one of:
- Normal: {{"schema_version": 1, "outcome": "plan_needed", "summary": "<one paragraph:
  what the mission needs and why it is normal complexity>"}}
  Optionally, if while triaging you have ALREADY fully formed the implementation plan
  (never force this), also write it to /workspace/out/PLAN.md — a complete, standalone
  markdown plan an implementer can execute without further context.
- Trivial: implement it in /workspace/repo (commit at the very end only; branch
  `devcake/{key}`; push; open a PR), then {{"schema_version": 1, "outcome":
  "executed_trivially", "summary": "...", "pr_url": "..."}}
- High: {{"schema_version": 1, "outcome": "decomposed", "summary": "...",
  "decomposition": [{{"title": "...", "description": "<standalone — reads as an
  independent mission, no references to siblings or 'this mission'>",
  "priority": "urgent|high|medium|low"}}, ...]}}
  Never decompose a mission whose labels include DEVCAKE-CREATED.

Your final message should be a concise assessment summary — it becomes part of the
mission's permanent transcript in the PMO system.
"""


def onboard_prompt(identifying_prompt: str, mission: Mission) -> str:
    return identifying_prompt + "\n" + ONBOARD_PLAYBOOK.format(
        key=mission.key, priority=mission.priority, url=mission.url,
        title=mission.title, description=mission.description or "(no description)")
