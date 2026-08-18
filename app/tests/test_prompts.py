"""INV-6 (commit only at the end, work only in the clone) — the binding rules
must appear verbatim in every playbook that writes code (docs/03 §7)."""
from datetime import datetime, timezone

from devcake.domain.model import Mission
from devcake.adapters.github import GitHubForge
from devcake.adapters.gitlab import GitLabForge
from devcake.prompts import (execute_prompt, steward_prompt, onboard_prompt,
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


def test_execute_binding_rule_one_scopes_writes_not_outputs():
    """ADR-0018: rule 1 used to read "Work ONLY inside /workspace/repo/…",
    contradicting rule 7's /workspace/out/result.json — weaker models resolved
    the contradiction by writing result.json inside the clone, which fails the
    run as DEV_BAD_OUTPUT and can be swept into the PR by commit-at-end. The
    rescoped rule restricts only the files the Dev CHANGES and names
    /workspace/out/ as the required output destination."""
    from devcake.prompts import DEFAULT_PLAYBOOKS
    from devcake.prompts.customer_success import CS_PLAYBOOKS
    for pb in (DEFAULT_PLAYBOOKS["EXECUTE"], CS_PLAYBOOKS["EXECUTE"]):
        assert "Work ONLY inside" not in pb
        assert "and nowhere else" in pb          # still scoped to ONE repo
        assert "does NOT restrict where your outputs go." in pb
        assert "required destination for result.json (rule 7)" in pb
        assert "Never write result.json into the repository." in pb
        assert "/workspace/out/" in pb
    p = execute_prompt("ID", M, "repo", GH_PR)    # and in the rendered prompt
    assert "Work ONLY inside" not in p
    assert "does NOT restrict where your outputs go." in p
    assert "/workspace/out/result.json" in p


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


def test_onboard_decomposition_rule_interpolated():
    """ADR-0012: the depth rule is per-mission ({decomposition_rule}), no
    longer the static DEVCAKE-CREATED sentence; operator templates without
    the placeholder degrade gracefully (identical render for any rule)."""
    p = onboard_prompt("ID", M, decomposition_rule="RULE-SENTINEL-42")
    assert "RULE-SENTINEL-42" in p
    assert "DEVCAKE-CREATED" not in p
    legacy = "no placeholder here {key}"
    assert (onboard_prompt("ID", M, playbook=legacy, decomposition_rule="A")
            == onboard_prompt("ID", M, playbook=legacy,
                              decomposition_rule="B"))


def test_default_onboard_playbooks_carry_decomposition_rule():
    from devcake.prompts import DEFAULT_PLAYBOOKS
    from devcake.prompts.customer_success import CS_PLAYBOOKS
    for pb in (DEFAULT_PLAYBOOKS["ONBOARD"], CS_PLAYBOOKS["ONBOARD"]):
        assert "{decomposition_rule}" in pb
        assert "DEVCAKE-CREATED" not in pb


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


def test_turn_discipline_in_result_writing_playbooks():
    """ADR-0022 PR-1: weaker models end a turn to narrate an intention; a
    turn without a tool call is the harness's end-of-run signal, so the run
    dies as exit 11 mid-mission (docs/15 §2b). The epilogue rides every
    playbook that must WRITE result.json — code-owned, so it survives
    operator template overrides. PLAN is excluded: read-only, its final
    message IS the deliverable, ending the turn is correct there."""
    writing = (onboard_prompt("ID", M), execute_prompt("ID", M, "repo", GH_PR),
               review_prompt("ID", M), steward_prompt("ID", [M]))
    for p in writing:
        assert "Never end your turn" in p
        assert "call a tool" in p
        assert "verify that /workspace/out/result.json exists" in p
    assert "Never end your turn" not in plan_prompt("ID", M)


def test_discoveries_epilogue_in_authoring_playbooks():
    """ADR-0033 D1: the discoveries contract rides every result.json author
    (code-owned, survives operator templates). PLAN cannot author (its
    result is entrypoint-synthesized — the relay below is its channel) and
    STEWARD never authors (Decision 7's chain-reaction damper)."""
    for p in (onboard_prompt("ID", M), execute_prompt("ID", M, "repo", GH_PR),
              review_prompt("ID", M)):
        assert '"discoveries"' in p
        assert '"about"' in p                     # optional topic tags
        assert "memory this otherwise memoryless system" in p
        assert "an entry without evidence is an opinion" in p
        assert "At most 3 discoveries" in p       # default cap rendered
    assert '"discoveries"' not in plan_prompt("ID", M)
    assert '"discoveries"' not in steward_prompt("ID", [M])


def test_discoveries_cap_wording_tracks_the_knob():
    assert "At most 5 discoveries" in execute_prompt(
        "ID", M, "repo", GH_PR, discoveries_cap=5)
    unlimited = execute_prompt("ID", M, "repo", GH_PR, discoveries_cap=0)
    assert "At most" not in unlimited.split("### Discoveries")[1] \
        .split("###")[0]
    assert "self-regulate" in unlimited


def _flat(text: str) -> str:
    """Whitespace-normalized view — playbook prose wraps at 79 cols, so
    contract assertions must not depend on where the line breaks fall."""
    return " ".join(text.split())


def test_plan_relay_and_execute_carry_forward():
    """ADR-0033 D1 relay: PLAN's off-mission findings ride a marked PLAN.md
    section; EXECUTE verifies and carries them into its own discoveries."""
    from devcake.prompts import EXECUTE_PLAYBOOK, PLAN_PLAYBOOK
    assert "## Findings beyond this mission" in _flat(PLAN_PLAYBOOK)
    assert "leads for the pipeline, not plan content" in _flat(PLAN_PLAYBOOK)
    assert "Findings beyond this mission" in _flat(EXECUTE_PLAYBOOK)
    assert "verify each finding's evidence yourself" in _flat(EXECUTE_PLAYBOOK)


def test_handoff_carries_discovery_consequences_downstream():
    """Founder ruling (2026-08-13): a handoff is a DELIVERY METHOD for
    discoveries that matter immediately downstream — the successor must not
    block on asynchronous routing. The contract instructs the duplication
    instead of forbidding it."""
    from devcake.prompts import REVIEW_PLAYBOOK
    from devcake.prompts.customer_success import CS_PLAYBOOKS
    assert "carry its consequence here too" in _flat(REVIEW_PLAYBOOK)
    assert "must not wait on it" in _flat(REVIEW_PLAYBOOK)
    assert "carry its consequence here too" in _flat(CS_PLAYBOOKS["REVIEW"])
    # the old phrasing that made handoff the discovery record is gone
    assert "what was DISCOVERED along the way" not in _flat(REVIEW_PLAYBOOK)


def test_steward_discovery_prompt_contract():
    """ADR-0033 PR-2: the discovery flavor's code-owned playbook — package
    embedded, laminarity test, propose-only routes under the duty-agnostic
    `stewarded` outcome, TURN_DISCIPLINE riding along, and NEVER the
    authoring epilogue (Decision 7 chain-reaction damper)."""
    from devcake.prompts import steward_discovery_prompt
    p = steward_discovery_prompt("ID", "PACKAGE-SENTINEL")
    assert "PACKAGE-SENTINEL" in p
    assert "laminarity" in p.lower()
    assert '"outcome": "stewarded"' in p
    assert '"routes"' in p and '"declined"' in p
    assert "Verbatim transport" in p
    assert "Never end your turn" in p
    assert '"discoveries"' not in p


def test_steward_prompt_embeds_missions():
    a = Mission(instance="linear", pmo_id="ida", pmo_kind="issue", key="T-1", title="write docs",
                description="x" * 500, status="backlog",
                updated_at=datetime.now(timezone.utc))
    b = Mission(instance="linear", pmo_id="idb", pmo_kind="issue", key="T-2", title="implement",
                status="backlog", blocked_by=["ida"],
                updated_at=datetime.now(timezone.utc))
    p = steward_prompt("ID", [a, b])
    assert "T-1" in p and "T-2" in p
    assert "blocked by: T-1" in p                 # existing blockers shown as keys
    assert "x" * 300 in p and "x" * 301 not in p  # description head capped
    assert "stewarded" in p


def test_steward_prompt_operator_template_and_code_owned_contract():
    """2026-08-14: the relations instruction half is operator-editable
    (config.Steward.playbook_template); the result contract is appended
    by code so no edit can break the machine half. Braces in operator
    text must survive (replace, not str.format)."""
    from devcake.prompts import STEWARD_RESULT_CONTRACT, steward_prompt
    m = Mission(instance="linear", pmo_id="p1", pmo_kind="issue",
                key="T-1", title="t", description="d", status="backlog",
                updated_at=datetime.now(timezone.utc))
    custom = 'Only map {"json": true} style pairs.\n{mission_table}'
    p = steward_prompt("ID", [m], template=custom)
    assert 'Only map {"json": true} style pairs.' in p
    assert "T-1" in p                                  # table substituted
    assert "Required output" in p                      # contract appended
    assert STEWARD_RESULT_CONTRACT.strip() in p
    # a template that forgot the placeholder still receives the table
    p2 = steward_prompt("ID", [m], template="Be gentle.")
    assert "T-1" in p2 and "Be gentle." in p2
    # empty template falls back to the shipped default
    p3 = steward_prompt("ID", [m], template="  ")
    assert "RELATIONS STEWARD" in p3
