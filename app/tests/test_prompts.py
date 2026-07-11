"""INV-6 (commit only at the end, work only in the clone) — the binding rules
must appear verbatim in every playbook that writes code (docs/03 §7)."""
from datetime import datetime, timezone

from devcake.pmo import Mission
from devcake.prompts import execute_prompt, onboard_prompt

M = Mission(pmo_id="x", pmo_kind="issue", key="T-9", title="t", status="backlog",
            updated_at=datetime.now(timezone.utc))


def test_execute_playbook_binding_rules_inv6():
    p = execute_prompt("ID", M, "repo")
    assert "Commit ONLY at the very end" in p
    assert "NEVER force-push" in p
    assert "devcake/T-9" in p
    assert "result.json" in p


def test_execute_forge_variants():
    assert "gh pr" in execute_prompt("ID", M, "repo", forge="github")
    assert "glab mr" in execute_prompt("ID", M, "repo", forge="gitlab")


def test_onboard_depth_limit_stated():
    assert "DEVCAKE-CREATED" in onboard_prompt("ID", M)
