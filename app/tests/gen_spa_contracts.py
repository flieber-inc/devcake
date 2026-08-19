"""Generator for docs/contracts/spa-contracts.json (ADR-0034; 2026-08-12
audit: the SPA hand-mirrors backend contracts — board derivation, instance-
name rule, card scaffolds, plus CAKE-88 vocabularies — with "Mirrors X"
comments and no cross-language test).

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
from typing import get_args

from devcake.api.mission_actions import ACTION_SPECS
from devcake.config import (AppConfig, HARNESS_VAR_PATTERN, _CRON_ID_RE,
                            _INSTANCE_NAME_RE, PMOInstance, RepoInstance,
                            SkillSource)
from devcake.domain.model import LABEL_MERGE, Mission, MissionType, derive
from devcake.domain.run import TERMINAL_STATES
from devcake.secrets import CONNECTION_FIELDS

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
# Derived from model_fields so a new server field without a scaffold update
# turns the spa-contracts exhaustiveness pin red (identity keys are the
# scaffold constructor args, not defaults-map entries).
_PMO_CARD_FIELDS = tuple(
    k for k in PMOInstance.model_fields if k not in ("name", "system"))
_REPO_CARD_FIELDS = tuple(
    k for k in RepoInstance.model_fields if k not in ("name", "url"))
_SKILL_SOURCE_CARD_FIELDS = tuple(
    k for k in SkillSource.model_fields if k not in ("name",))


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


def _literal_values(field_name: str) -> list[str]:
    """Sorted Literal members from AppConfig — never hand-type the strings."""
    return sorted(get_args(AppConfig.model_fields[field_name].annotation))


def _action_preconditions() -> dict[str, dict[str, list[str]]]:
    """Serializable ACTION_SPECS require_present/require_absent, plus
    force_freshness (outside ACTION_SPECS; 409s without DEVCAKE-MERGE)."""
    out: dict[str, dict[str, list[str]]] = {}
    for action, spec in ACTION_SPECS.items():
        out[action] = {
            "require_present": sorted(spec.require_present),
            "require_absent": sorted(spec.require_absent),
        }
    # Not in ACTION_SPECS — attach beside so the SPA menu pin covers MERGE.
    out["force_freshness"] = {
        "require_present": [LABEL_MERGE],
        "require_absent": [],
    }
    return out


def build() -> dict:
    reasons, vectors = _board_vectors()
    terminal = sorted(TERMINAL_STATES)
    return {
        "_generated_by": "app/tests/gen_spa_contracts.py — do not hand-edit",
        "instance_name_re": _INSTANCE_NAME_RE,
        "harness_var_pattern": HARNESS_VAR_PATTERN,
        "cron_id_re": _CRON_ID_RE,
        "continuation_policies": _literal_values("continuation_policy"),
        "attempt_reset_policies": _literal_values("attempt_reset"),
        "mission_stages": [m.value for m in MissionType],
        "connection_fields": {
            scope: sorted(fields)
            for scope, fields in CONNECTION_FIELDS.items()},
        "action_preconditions": _action_preconditions(),
        "run_terminal_states": terminal,
        "run_stopped_states": sorted(set(terminal) | {"finalizing"}),
        "pmo_card_defaults": {
            k: PMOInstance(name="x", team_key="").model_dump()[k]
            for k in _PMO_CARD_FIELDS},
        "repo_card_defaults": {
            k: RepoInstance(name="x", url="https://h/o/r").model_dump()[k]
            for k in _REPO_CARD_FIELDS},
        "skill_source_card_defaults": {
            k: SkillSource(name="x").model_dump()[k]
            for k in _SKILL_SOURCE_CARD_FIELDS},
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
