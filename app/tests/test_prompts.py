"""INV-6 (commit only at the end, work only in the clone) — the binding rules
must appear verbatim in every playbook that writes code (docs/03 §7)."""
from datetime import datetime, timezone

from devcake.domain.model import Mission
from devcake.adapters.github import GitHubForge
from devcake.adapters.gitlab import GitLabForge
from devcake.prompts import (execute_prompt, mapper_prompt, onboard_prompt,
                             plan_prompt, review_prompt)

GH_PR = GitHubForge.descriptor.pr_instructions
GL_PR = GitLabForge.descriptor.pr_instructions

M = Mission(instance="linear", pmo_id="x", pmo_kind="issue", key="T-9", title="t", status="backlog",
            updated_at=datetime.now(timezone.utc))


def test_execute_playbook_binding_rules_inv6():
    p = execute_prompt("ID", M, "repo", GH_PR)
    assert "Commit ONLY at the very end" in p
    assert "NEVER force-push" in p
    assert "devcake/LINEAR-T-9" in p
    assert "result.json" in p


def test_execute_playbook_syncs_default_branch():
    # docs/03 §4.1 prevention rule: PRs arrive mergeable
    p = execute_prompt("ID", M, "repo", GH_PR)
    assert "git fetch origin && git merge origin/main" in p
    assert "Never rebase" in p
    p2 = execute_prompt("ID", M, "repo", GH_PR, default_branch="develop")
    assert "git merge origin/develop" in p2


def test_execute_playbook_conflict_directive_awareness():
    # a 🔀 resolve directive overrides the normal implement-the-mission job
    p = execute_prompt("ID", M, "repo", GH_PR)
    assert "conflict-resolve directive" in p
    assert "do NOT redo or extend" in p


def test_execute_forge_variants():
    assert "gh pr" in execute_prompt("ID", M, "repo", GH_PR)
    assert "glab mr" in execute_prompt("ID", M, "repo", GL_PR)


def test_onboard_depth_limit_stated():
    assert "DEVCAKE-CREATED" in onboard_prompt("ID", M)


def test_onboard_declares_blocked_by():
    p = onboard_prompt("ID", M)
    assert "blocked_by" in p
    assert "Only earlier parts may be referenced" in p


def test_human_handoff_in_writing_playbooks():
    # PLAN can't emit outcomes (plan mode is read-only; the entrypoint
    # synthesizes its result.json) — every other playbook must offer the exit
    for p in (onboard_prompt("ID", M), execute_prompt("ID", M, "repo", GH_PR),
              review_prompt("ID", M)):
        assert "human_needed" in p
    assert "human_needed" not in plan_prompt("ID", M)


def test_human_comments_note_everywhere():
    for p in (onboard_prompt("ID", M), plan_prompt("ID", M),
              execute_prompt("ID", M, "repo", GH_PR), review_prompt("ID", M)):
        assert "🧑 HUMAN" in p


def test_mapper_prompt_embeds_missions():
    a = Mission(instance="linear", pmo_id="ida", pmo_kind="issue", key="T-1", title="write docs",
                description="x" * 500, status="backlog",
                updated_at=datetime.now(timezone.utc))
    b = Mission(instance="linear", pmo_id="idb", pmo_kind="issue", key="T-2", title="implement",
                status="backlog", blocked_by=["ida"],
                updated_at=datetime.now(timezone.utc))
    p = mapper_prompt("ID", [a, b])
    assert "T-1" in p and "T-2" in p
    assert "blocked by: T-1" in p                 # existing blockers shown as keys
    assert "x" * 300 in p and "x" * 301 not in p  # description head capped
    assert "relations_mapped" in p
