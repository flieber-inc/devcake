"""Playbook prompts (docs/03 §7). The playbook restates the binding rules from
docs/03 — workspace boundaries, result.json contract, bounded effort. Prompts
inline only the mission title/description; activity/ (MISSION.md brief +
ACTIVITY.md feed mirror + attached files, ADR-0014) is reference material.

Operator-editable templates (v0.1.1): each Mission Type's playbook can be
replaced by a stored template (prompts/templates.py; /data/config/
prompt_templates/). Templates use the SAME {var} placeholders as the
constants below, rendered by render_playbook — which substitutes ONLY the
allowlisted variable names and leaves every other brace literal, so raw JSON
examples need no {{escaping}} in stored templates. The Python constants
remain the seed source; DEFAULT_PLAYBOOKS un-doubles their str.format-era
double braces once, at import.
"""

import re

from ..domain.model import Mission
from ..ports.forge import mission_branch

# variables each Mission Type's template may reference (the validation
# allowlist AND exactly what dispatch provides). MAPPER is deliberately not
# templated (founder decision 2026-07-14); adding it later = one entry here
# + one DEFAULT_PLAYBOOKS entry.
PLAYBOOK_VARS: dict[str, tuple[str, ...]] = {
    "ONBOARD": ("key", "priority", "url", "title", "branch", "description",
                "project_note", "repo_options", "reference_repos",
                "decomposition_rule"),
    "PLAN": ("key", "priority", "url", "title", "description",
             "reference_repos"),
    "EXECUTE": ("key", "priority", "url", "title", "repo_name",
                "pr_instructions", "default", "branch", "description",
                "reference_repos"),
    "REVIEW": ("key", "priority", "url", "title", "branch", "description",
               "reference_repos"),
}

_VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_playbook(template: str, values: dict) -> str:
    """Substitute {var} for vars present in `values`; every other brace is
    literal (never raw str.format over operator text — the playbooks are full
    of literal JSON). Replacement values are inserted verbatim (a function
    repl disables re.sub escape processing) and never re-scanned."""
    return _VAR.sub(
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
        template)


def _undouble(s: str) -> str:
    # the module constants escape literal braces for str.format; stored
    # templates (and render_playbook) treat braces literally
    return s.replace("{{", "{").replace("}}", "}")

ONBOARD_PLAYBOOK = """
## Your current mission type: ONBOARD (triage)

Assess the complexity of the mission below against the actual codebase, then
route it. **Assess, don't deep-dive** — this is a bounded triage pass, not an
exploration. Do not modify any code in this mission type (the rare trivial
path is the only exception, and you must be certain).

### Workspace
- `/workspace/repo/` — a fresh clone of the repository. The ONLY place work may happen.
- `/workspace/activity/` — the mission's knowledge base: MISSION.md (the
  brief), ACTIVITY.md (a faithful mirror of the mission's feed — full posts,
  replies, `[attachment: …]` markers), and every attached file — including
  prior steps' full session transcripts (`N_TYPE.md`). Reference material:
  grep or read what you need; do not assume you must read all of it.
- `/workspace/out/` — where your outputs go.

### The mission
- Key: {key}   ·   Priority: {priority}   ·   URL: {url}
- Title: **{title}**

{description}

{project_note}{repo_options}{reference_repos}### Classify (docs rubric)
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
  `{branch}`; push; open a PR), then {{"schema_version": 1, "outcome":
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
  {decomposition_rule}

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


# {decomposition_rule} wordings (ADR-0012): dispatch picks by the mission's
# PMO-recorded depth vs the configured limit (dispatch._decomposition_rule).
# Operator templates missing the placeholder keep their stored sentence —
# over-restrictive at worst, never a wasted run.
DECOMPOSITION_RULE_UNLIMITED = (
    "Decomposition is not depth-limited on this instance: `decomposed` is "
    "available even for missions that were themselves created by "
    "decomposition. Use your judgment — prefer the smallest breakdown that "
    "yields independently executable parts.")
DECOMPOSITION_RULE_ALLOWED = (
    "This mission is at decomposition depth {depth} of a limit of {limit}: "
    "`decomposed` is available if the work is genuinely compound.")
DECOMPOSITION_RULE_AT_LIMIT = (
    "This mission is AT the decomposition depth limit ({limit}): the "
    "`decomposed` outcome is FORBIDDEN here and the app will reject it. "
    "Classify trivial or normal — or hand off to a human if it is truly "
    "too large to proceed as one mission.")


PROJECT_NOTE = """### This mission is a PROJECT
Projects ALWAYS take the high-complexity path (never trivial, never normal):
decompose it into standalone child issues covering the full extent of the work.

"""


def onboard_prompt(identifying_prompt: str, mission: Mission,
                   playbook: str | None = None,
                   repo_options: str = "",
                   reference_repos: str = "",
                   decomposition_rule: str = "") -> str:
    """repo_options: the multi-repo triage section (item 2 full scope) —
    dispatch builds it from the instance's repo set; empty for single-repo
    and zero-repo instances (renders to nothing, like project_note).
    decomposition_rule: the per-mission depth line (ADR-0012) — dispatch
    computes it via _decomposition_rule; empty renders to nothing."""
    text = render_playbook(
        playbook if playbook is not None else DEFAULT_PLAYBOOKS["ONBOARD"],
        {"key": mission.key, "priority": mission.priority, "url": mission.url,
         "title": mission.title,
         "branch": mission_branch(mission.instance, mission.key),
         "description": mission.description or "(no description)",
         "project_note": PROJECT_NOTE if mission.pmo_kind == "project" else "",
         "repo_options": repo_options,
         "reference_repos": reference_repos,
         "decomposition_rule": decomposition_rule})
    return identifying_prompt + "\n" + text + HUMAN_HANDOFF + HUMAN_COMMENTS_NOTE


PLAN_PLAYBOOK = """
## Your current mission type: PLAN

Produce a complete, standalone implementation plan for the mission below — and
nothing else. You are running in the harness's read-only plan mode; your final
message IS the plan and will be delivered verbatim to the implementer, who has
no other context. Structure it so a competent engineer (or agent) can execute
it without asking questions: goals, file-by-file changes, new files, testing
strategy, and acceptance checks. Study /workspace/repo, the brief in
/workspace/activity/MISSION.md, and the mission history in
/workspace/activity/ (reference material — read what you need).

### The mission
- Key: {key}   ·   Priority: {priority}   ·   URL: {url}
- Title: **{title}**

{description}
{reference_repos}"""


def plan_prompt(identifying_prompt: str, mission: Mission,
                playbook: str | None = None,
                reference_repos: str = "") -> str:
    text = render_playbook(
        playbook if playbook is not None else DEFAULT_PLAYBOOKS["PLAN"],
        {"key": mission.key, "priority": mission.priority, "url": mission.url,
         "title": mission.title,
         "description": mission.description or "(no description)",
         "reference_repos": reference_repos})
    return identifying_prompt + "\n" + text + HUMAN_COMMENTS_NOTE


EXECUTE_PLAYBOOK = """
## Your current mission type: EXECUTE

Implement the mission's plan. The latest plan (PLAN*.md) and any review reports
are in /workspace/activity/ — read the plan first; if a review report exists,
its findings take priority. The mission brief is /workspace/activity/MISSION.md.
Where reality contradicts the plan, implement the smallest sound deviation and
document it in your summary.

SPECIAL CASE — conflict-resolve directive: if the most recent DevCake entry in
/workspace/activity/ACTIVITY.md is a 🧩 conflict-resolve directive, your ONLY
job is to sync `{branch}` with the default branch, resolve the merge
conflicts, and push — do NOT redo or extend the mission's implementation.

### Binding rules (violations fail the run)
1. Work ONLY inside /workspace/repo/{repo_name}/.
2. Branch: `{branch}`. If it exists on the remote, check it out and
   continue on it (`git fetch origin {branch} && git checkout {branch}`);
   otherwise create it from the default branch. NEVER force-push.
3. Run the repo's tests/build if present; add tests per the plan.
4. Before your final commit, sync with the default branch so the PR arrives
   mergeable: `git fetch origin && git merge origin/{default}`, resolving any
   conflicts locally (keep the default branch's state for code your mission
   didn't change). Never rebase.
5. Commit ONLY at the very end, one commit: `[{key}] <concise summary>`.
   Then push: `git push -u origin {branch}` (credentials are configured).
6. {pr_instructions}
7. Write /workspace/out/result.json EXACTLY as:
   {{"schema_version": 1, "outcome": "executed", "summary": "<what you built,
   deviations, test results>", "pr_url": "<the PR/MR url>"}}

### The mission
- Key: {key}   ·   Priority: {priority}   ·   URL: {url}
- Title: **{title}**

{description}
{reference_repos}"""


def execute_prompt(identifying_prompt: str, mission: Mission, repo_name: str,
                   pr_instructions: str, default_branch: str = "main",
                   playbook: str | None = None,
                   reference_repos: str = "") -> str:
    """pr_instructions is the forge descriptor's CLI-dialect template
    (docs/06) — placeholders: {key} {title} {default} {branch}. It is
    code-owned, so it keeps str.format; its rendered result becomes the
    {pr_instructions} variable of the (possibly operator-edited) playbook."""
    branch = mission_branch(mission.instance, mission.key)
    pr = pr_instructions.format(key=mission.key, title=mission.title,
                                default=default_branch, branch=branch)
    text = render_playbook(
        playbook if playbook is not None else DEFAULT_PLAYBOOKS["EXECUTE"],
        {"key": mission.key, "priority": mission.priority, "url": mission.url,
         "title": mission.title, "repo_name": repo_name,
         "pr_instructions": pr, "default": default_branch, "branch": branch,
         "description": mission.description or "(no description)",
         "reference_repos": reference_repos})
    return identifying_prompt + "\n" + text + HUMAN_HANDOFF + HUMAN_COMMENTS_NOTE


REVIEW_PLAYBOOK = """
## Your current mission type: REVIEW

Act as a skeptical software engineer reviewing the work delivered for this
mission. Rubber-stamping is forbidden — your default posture is distrust; an
approval must be EARNED by the evidence you gather.

### Procedure (binding)
1. The work lives on branch `{branch}` — check it out:
   `git fetch origin {branch} && git checkout {branch}`.
2. Read the plan and any prior review reports in /workspace/activity/ and diff
   the branch against the default branch. Judge the work against the PLAN and
   the MISSION (brief: /workspace/activity/MISSION.md) — flag omissions, not
   just bugs.
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
{reference_repos}"""


def review_prompt(identifying_prompt: str, mission: Mission,
                  playbook: str | None = None,
                  reference_repos: str = "") -> str:
    text = render_playbook(
        playbook if playbook is not None else DEFAULT_PLAYBOOKS["REVIEW"],
        {"key": mission.key, "priority": mission.priority, "url": mission.url,
         "title": mission.title,
         "branch": mission_branch(mission.instance, mission.key),
         "description": mission.description or "(no description)",
         "reference_repos": reference_repos})
    return identifying_prompt + "\n" + text + HUMAN_HANDOFF + HUMAN_COMMENTS_NOTE


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

# the canonical (un-doubled) playbook texts — seed source for the stored
# "default" templates and the fallback when a stored template is broken
DEFAULT_PLAYBOOKS: dict[str, str] = {
    "ONBOARD": _undouble(ONBOARD_PLAYBOOK),
    "PLAN": _undouble(PLAN_PLAYBOOK),
    "EXECUTE": _undouble(EXECUTE_PLAYBOOK),
    "REVIEW": _undouble(REVIEW_PLAYBOOK),
}


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
