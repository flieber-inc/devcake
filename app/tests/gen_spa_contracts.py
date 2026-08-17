"""Generator for docs/contracts/spa-contracts.json (ADR-0034; 2026-08-12
audit: the SPA hand-mirrors backend contracts — board derivation
precedence, the instance-name rule, the card scaffolds, and the
connections-registry offline FALLBACK — with "Mirrors X" comments and no
cross-language test until pinned).

The committed JSON is generated FROM THE PYTHON SOURCE OF TRUTH here and
pinned by both suites: test_spa_contracts.py asserts the file matches a
fresh generation (a backend change turns pytest red until regenerated), and
admin/spa/tests/contracts.mjs replays it against the SPA libs (an SPA drift
turns the node suite red). Regenerate with:

    ./scripts/pytest_app.sh tests/test_spa_contracts.py --collect-only -q  # (sanity)
    docker run --rm -w /srv devcake/app-test:latest \
        python -m tests.gen_spa_contracts > docs/contracts/spa-contracts.json
"""

from __future__ import annotations

import itertools
import json

from devcake.adapters.registry import connections_registry_payload
from devcake.config import (HARNESS_VAR_PATTERN, _INSTANCE_NAME_RE,
                            PMOInstance, RepoInstance)
from devcake.domain.model import Mission, derive

from datetime import datetime, timezone

# Every label that participates in the derivation table — the vector sweep
# is the full powerset of these, so ANY precedence reordering in derive()
# changes some vector's reason.
_LABELS = ("DEVCAKE", "DEVCAKE-PLAN", "DEVCAKE-EXECUTE", "DEVCAKE-REVIEW",
           "DEVCAKE-MERGE", "DEVCAKE-SKIP", "DEVCAKE-FAILED",
           "DEVCAKE-NEEDS-HUMAN")
_STATUSES = ("backlog", "in_progress", "done", "canceled")
_ADOPTION = ("opt_in", "opt_out")

# SPA scaffold fields (cards.js) — the convergence contract is scaffold ==
# refetched server row, so the defaults come from the pydantic models.
_PMO_CARD_FIELDS = ("team_key", "api_base", "repos", "reference_repos",
                    "memory_repos",
                    "intake_paused", "discovery_routing", "assignments",
                    "managed")
_REPO_CARD_FIELDS = ("forge", "api_base", "default_branch", "auto_merge",
                     "auto_resolve_merge_conflicts",
                     "merge_settle_minutes",
                     "merge_retry_window_minutes")


def _board_vectors() -> tuple[list[str], list[list]]:
    """(reasons, vectors) — vector = [status_i, adoption_i, [labels], reason_i]
    with indexed reasons to keep the committed file compact."""
    reasons: list[str] = []
    idx: dict[str, int] = {}
    vectors: list[list] = []
    now = datetime.now(timezone.utc)
    for status, adoption in itertools.product(_STATUSES, _ADOPTION):
        for r in range(len(_LABELS) + 1):
            for combo in itertools.combinations(_LABELS, r):
                m = Mission(pmo_id="1", pmo_kind="issue", key="T-1",
                            title="t", description="", status=status,
                            labels=set(combo), updated_at=now, url="",
                            instance="x")
                reason = derive(m, adoption).reason
                if reason not in idx:
                    idx[reason] = len(reasons)
                    reasons.append(reason)
                vectors.append([_STATUSES.index(status),
                                _ADOPTION.index(adoption),
                                sorted(combo), idx[reason]])
    return reasons, vectors


def build() -> dict:
    reasons, vectors = _board_vectors()
    return {
        "_generated_by": "app/tests/gen_spa_contracts.py — do not hand-edit",
        "instance_name_re": _INSTANCE_NAME_RE,
        "harness_var_pattern": HARNESS_VAR_PATTERN,
        "pmo_card_defaults": {
            k: PMOInstance(name="x", team_key="").model_dump()[k]
            for k in _PMO_CARD_FIELDS},
        "repo_card_defaults": {
            k: RepoInstance(name="x", url="https://h/o/r").model_dump()[k]
            for k in _REPO_CARD_FIELDS},
        # Offline SPA FALLBACK (admin/spa/src/lib/registry.js) must match
        # this object field-for-field — contracts.mjs asserts equality.
        "connections_registry": connections_registry_payload(),
        "board": {
            "statuses": list(_STATUSES),
            "adoption_modes": list(_ADOPTION),
            "reasons": reasons,
            "vectors": vectors,
        },
    }


def render() -> str:
    # compact single-line: the consumers parse JSON (comparison is on the
    # parsed object, never bytes), and regeneration diffs as one line
    return json.dumps(build(), separators=(",", ":"),
                      ensure_ascii=False) + "\n"


if __name__ == "__main__":
    print(render(), end="")
