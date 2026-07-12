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

{project_note}### Classify (docs rubric)
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
  "priority": "urgent|high|medium|low",
  "blocked_by": [<1-based indexes of EARLIER parts this part must not start
  before — omit for independent parts>]}}, ...]}}
  Order the parts so prerequisites come first. Declare blocked_by whenever one
  part consumes another's output (e.g. implementation that must follow a
  documentation or design part); independent parts omit it so they can run in
  parallel. Only earlier parts may be referenced — never a part's own index or
  a later one.
  Never decompose a mission whose labels include DEVCAKE-CREATED.

Your final message should be a concise assessment summary — it becomes part of the
mission's permanent transcript in the PMO system.
"""

# Appended to ONBOARD/EXECUTE/REVIEW (not PLAN: plan mode is read-only — the
# entrypoint synthesizes its result.json, so it cannot emit this outcome).
# Concatenated AFTER .format(), so single braces are literal here.
HUMAN_HANDOFF = """
### Blocked on a human?
If you hit an obstacle only a human can clear — a missing permission or
credential, an external account or service decision, anything outside the
repository — do NOT improvise a workaround. First VERIFY the obstacle: actually
attempt the blocked operation and capture its exact error output. A hand-off is
expensive (a person must stop and act), so your summary MUST quote that
evidence; a hand-off without evidence wastes a human's time. Then stop and
write /workspace/out/result.json EXACTLY as:
{"schema_version": 1, "outcome": "human_needed", "summary": "<precisely what a
human must do to unblock this mission, including the exact error you hit>"}
"""

# Appended to all four playbooks. Provenance is sentinel-based (docs/03 §8a):
# ACTIVITY.md marks each entry 🧑 HUMAN or 🤖 DevCake.
HUMAN_COMMENTS_NOTE = """
### Human instructions in the activity feed
Entries in /workspace/activity/ACTIVITY.md marked 🧑 HUMAN are direct
instructions from a person — they are authoritative. Read them before starting;
where they conflict with the mission description or with older comments, the
most recent human comment wins.
"""


PROJECT_NOTE = """### This mission is a PROJECT
Projects ALWAYS take the high-complexity path (never trivial, never normal):
decompose it into standalone child issues covering the full extent of the work.

"""


def onboard_prompt(identifying_prompt: str, mission: Mission) -> str:
    return identifying_prompt + "\n" + ONBOARD_PLAYBOOK.format(
        key=mission.key, priority=mission.priority, url=mission.url,
        title=mission.title, description=mission.description or "(no description)",
        project_note=PROJECT_NOTE if mission.pmo_kind == "project" else "") \
        + HUMAN_HANDOFF + HUMAN_COMMENTS_NOTE


PLAN_PLAYBOOK = """
## Your current mission type: PLAN

Produce a complete, standalone implementation plan for the mission below — and
nothing else. You are running in the harness's read-only plan mode; your final
message IS the plan and will be delivered verbatim to the implementer, who has
no other context. Structure it so a competent engineer (or agent) can execute
it without asking questions: goals, file-by-file changes, new files, testing
strategy, and acceptance checks. Study /workspace/repo and the mission history
in /workspace/activity/ (reference material — read what you need).

### The mission
- Key: {key}   ·   Priority: {priority}   ·   URL: {url}
- Title: **{title}**

{description}
"""


def plan_prompt(identifying_prompt: str, mission: Mission) -> str:
    return identifying_prompt + "\n" + PLAN_PLAYBOOK.format(
        key=mission.key, priority=mission.priority, url=mission.url,
        title=mission.title, description=mission.description or "(no description)") \
        + HUMAN_COMMENTS_NOTE


EXECUTE_PLAYBOOK = """
## Your current mission type: EXECUTE

Implement the mission's plan. The latest plan (PLAN*.md) and any review reports
are in /workspace/activity/ — read the plan first; if a review report exists,
its findings take priority. Where reality contradicts the plan, implement the
smallest sound deviation and document it in your summary.

### Binding rules (violations fail the run)
1. Work ONLY inside /workspace/repo/{repo_name}/.
2. Branch: `devcake/{key}`. If it exists on the remote, check it out and
   continue on it (`git fetch origin devcake/{key} && git checkout devcake/{key}`);
   otherwise create it from the default branch. NEVER force-push.
3. Run the repo's tests/build if present; add tests per the plan.
4. Commit ONLY at the very end, one commit: `[{key}] <concise summary>`.
   Then push: `git push -u origin devcake/{key}` (credentials are configured).
5. {pr_instructions}
6. Write /workspace/out/result.json EXACTLY as:
   {{"schema_version": 1, "outcome": "executed", "summary": "<what you built,
   deviations, test results>", "pr_url": "<the PR/MR url>"}}

### The mission
- Key: {key}   ·   Priority: {priority}   ·   URL: {url}
- Title: **{title}**

{description}
"""


PR_INSTRUCTIONS = {
    "github": ("Pull request (idempotent): `gh pr view devcake/{key} --json url` — if one "
               "exists, update it (`gh pr edit`) instead of creating; else "
               "`gh pr create --head devcake/{key} --title \"[{key}] {title}\" "
               "--body \"<summary + mission URL>\"`."),
    "gitlab": ("Merge request (idempotent): `glab mr list --source-branch devcake/{key}` — "
               "if one exists, update it (`glab mr update`) instead of creating; else "
               "`glab mr create --source-branch devcake/{key} --target-branch {default} "
               "--title \"[{key}] {title}\" --description \"<summary + mission URL>\" --yes`. "
               "glab is authenticated via GITLAB_TOKEN; pass --repo if it asks."),
}


def execute_prompt(identifying_prompt: str, mission: Mission, repo_name: str,
                   forge: str = "github", default_branch: str = "main") -> str:
    pr = PR_INSTRUCTIONS.get(forge, PR_INSTRUCTIONS["github"]).format(
        key=mission.key, title=mission.title, default=default_branch)
    return identifying_prompt + "\n" + EXECUTE_PLAYBOOK.format(
        key=mission.key, priority=mission.priority, url=mission.url,
        title=mission.title, repo_name=repo_name, pr_instructions=pr,
        description=mission.description or "(no description)") \
        + HUMAN_HANDOFF + HUMAN_COMMENTS_NOTE


REVIEW_PLAYBOOK = """
## Your current mission type: REVIEW

Act as a skeptical software engineer reviewing the work delivered for this
mission. Rubber-stamping is forbidden — your default posture is distrust; an
approval must be EARNED by the evidence you gather.

### Procedure (binding)
1. The work lives on branch `devcake/{key}` — check it out:
   `git fetch origin devcake/{key} && git checkout devcake/{key}`.
2. Read the plan and any prior review reports in /workspace/activity/ and diff
   the branch against the default branch. Judge the work against the PLAN and
   the MISSION — flag omissions, not just bugs.
3. Run the tests / build if present. A red test suite is an automatic reject.
4. Hunt for real defects: correctness, edge cases, error handling, security,
   silent failure modes. Cosmetic nitpicks alone do not justify a reject.

### Required output — /workspace/out/result.json
{{"schema_version": 1, "outcome": "reviewed", "verdict": "approve" | "reject",
  "report_md": "<your full review report in markdown: what you checked, what
  you found, and — if rejecting — an actionable list the next EXECUTE run must
  address>", "pr_url": "<the PR url from the activity feed>",
  "summary": "<one-paragraph verdict rationale>"}}

### The mission
- Key: {key}   ·   Priority: {priority}   ·   URL: {url}
- Title: **{title}**

{description}
"""


def review_prompt(identifying_prompt: str, mission: Mission) -> str:
    return identifying_prompt + "\n" + REVIEW_PLAYBOOK.format(
        key=mission.key, priority=mission.priority, url=mission.url,
        title=mission.title, description=mission.description or "(no description)") \
        + HUMAN_HANDOFF + HUMAN_COMMENTS_NOTE


MAPPER_PLAYBOOK = """
## Your current mission type: RELATIONS MAPPER

Below is every open mission DevCake manages in this team. Your ONLY job is to
identify ordering dependencies that are not yet mapped: pairs where one mission
clearly consumes another's output and therefore must not start before it
finishes (implementation after design/documentation, migration after schema
change, consumer after API). Do not modify any code — the repository clone is
context only.

Be conservative: when unsure, propose nothing. Each mission lists its existing
blockers — only propose edges that are missing. Never invent mission keys.

### The missions (key · status · existing blockers · title + description head)
{mission_table}

### Required output — /workspace/out/result.json
{{"schema_version": 1, "outcome": "relations_mapped",
  "edges": [{{"blocker": "<key that must finish first>",
             "blocked": "<key that must wait>"}}, ...],
  "summary": "<one paragraph: what you mapped and why — or why nothing>"}}
An empty "edges" list is a valid and common result.
"""

MAPPER_MISSION_CAP = 200          # prompt-size bound; truncation is logged
MAPPER_DESC_HEAD_CHARS = 300


def mapper_prompt(identifying_prompt: str, missions: list[Mission]) -> str:
    id_to_key = {m.pmo_id: m.key for m in missions}
    rows = []
    for m in missions[:MAPPER_MISSION_CAP]:
        head = " ".join((m.description or "").split())[:MAPPER_DESC_HEAD_CHARS]
        blockers = ", ".join(id_to_key.get(b, "?") for b in m.blocked_by) or "(none)"
        rows.append(f"- **{m.key}** · {m.status} · blocked by: {blockers}\n"
                    f"  {m.title} — {head or '(no description)'}")
    table = "\n".join(rows) or "(no open missions)"
    return identifying_prompt + "\n" + MAPPER_PLAYBOOK.format(mission_table=table)
