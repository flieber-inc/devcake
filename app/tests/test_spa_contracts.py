"""docs/contracts/spa-contracts.json is generated from the Python source of
truth (gen_spa_contracts.build) and replayed by the SPA's pure-node suite —
the cross-language pin the 2026-08-12 audit found missing for the three
hand-mirrors (board derive precedence, instance-name rule, card scaffolds).
This side turns red when the BACKEND changes without regenerating."""

import json
from pathlib import Path

from tests.gen_spa_contracts import build

CONTRACTS = Path("/srv/docs/contracts/spa-contracts.json")
if not CONTRACTS.exists():
    CONTRACTS = (Path(__file__).resolve().parents[2]
                 / "docs" / "contracts" / "spa-contracts.json")


def test_contracts_file_matches_the_source_of_truth():
    assert CONTRACTS.exists(), (
        "mount missing — the pytest runner must bind docs → /srv/docs"
    )
    committed = json.loads(CONTRACTS.read_text())
    fresh = build()
    assert committed == fresh, (
        "docs/contracts/spa-contracts.json is stale vs the backend — "
        "regenerate: docker run --rm -v $(pwd)/app:/srv-rw -w /srv-rw "
        "devcake/app-test:latest python -m tests.gen_spa_contracts "
        "> docs/contracts/spa-contracts.json  (then re-run the SPA "
        "test:helpers suite, which replays the same file)"
    )


def test_card_default_maps_cover_every_non_identity_field():
    """Scaffold pins must stay exhaustive vs the live pydantic models — a
    new model field without a scaffold update must turn this red (ADR-0034).
    Identity keys are supplied by the SPA scaffold args, not the defaults
    map: PMO name/system, Repo name/url, SkillSource name."""
    from devcake.config import PMOInstance, RepoInstance, SkillSource

    data = build()
    assert set(data["pmo_card_defaults"]) == (
        set(PMOInstance.model_fields) - {"name", "system"})
    assert set(data["repo_card_defaults"]) == (
        set(RepoInstance.model_fields) - {"name", "url"})
    assert set(data["skill_source_card_defaults"]) == (
        set(SkillSource.model_fields) - {"name"})
    # Independent expected values: construct a real SkillSource and compare
    # defaults for the non-identity keys (do not re-read the generator tuple).
    dumped = SkillSource(name="x").model_dump()
    for key, value in data["skill_source_card_defaults"].items():
        assert value == dumped[key], key


def test_vector_sweep_is_the_full_powerset():
    """The drift guarantee rests on coverage: 4 statuses × 2 adoption modes
    × 2^8 label subsets — ANY precedence reordering in derive() must land
    in some vector."""
    data = build()
    assert len(data["board"]["vectors"]) == 4 * 2 * 256
    assert len(data["board"]["reasons"]) >= 8


def test_connections_registry_payload_domain_rules():
    """Independent domain rules for the registry projection (the single
    source behind GET /connections/registry and the SPA FALLBACK pin) — not
    a re-read of the same fields under a different name."""
    from devcake.adapters.registry import connections_registry_payload
    data = connections_registry_payload()
    # ALL_LABELS includes DEVCAKE-DISCOVERY (sweep gate); count is 11.
    assert data["managed_labels_expected"] == 11
    by_id = {s["id"]: s for s in data["pmo_systems"]}
    assert set(by_id) == {
        "linear", "gitea_issues", "github_issues", "gitlab_issues",
    }
    assert by_id["linear"]["supports_priority"] is True
    for forge_issue in ("gitea_issues", "github_issues", "gitlab_issues"):
        assert by_id[forge_issue]["supports_priority"] is False, forge_issue
    # Copy honesty: team_key_help must distinguish the issues board from a
    # per-mission work repo (forge-issue systems only).
    for forge_issue in ("gitea_issues", "github_issues", "gitlab_issues"):
        assert "Not a per-mission work repo" in by_id[forge_issue]["team_key_help"]
    assert by_id["github_issues"]["attachments_supported"] is False
    assert by_id["gitlab_issues"]["relations_supported"] is False
    assert "ghp_" in data["secret_shape_prefixes"]
    assert data["secret_shape_prefixes"] == sorted(data["secret_shape_prefixes"])


def test_discovery_label_is_derivation_inert():
    """ADR-0033 founder ruling 2a: DEVCAKE-DISCOVERY is a pure sweep gate —
    adding it to ANY label combination must leave derive() untouched (the
    label may never impede a mission's progression). Full powerset sweep,
    the executable twin of the AST guard in test_structure_guards."""
    import itertools
    from datetime import datetime, timezone

    from devcake.domain.model import LABEL_DISCOVERY, Mission, derive
    from tests.gen_spa_contracts import _ADOPTION, _LABELS, _STATUSES

    now = datetime.now(timezone.utc)
    for status, adoption in itertools.product(_STATUSES, _ADOPTION):
        for r in range(len(_LABELS) + 1):
            for combo in itertools.combinations(_LABELS, r):
                def _m(labels):
                    return Mission(pmo_id="1", pmo_kind="issue", key="T-1",
                                   title="t", description="", status=status,
                                   labels=set(labels), updated_at=now,
                                   url="", instance="x")
                base = derive(_m(combo), adoption)
                spiked = derive(_m(set(combo) | {LABEL_DISCOVERY}), adoption)
                assert base == spiked, (status, adoption, combo)


def test_continuation_and_attempt_reset_match_appconfig_literals():
    """CAKE-88: enum vocabularies ride spa-contracts from AppConfig Literals —
    independent get_args, not a re-read of the generator's own extraction."""
    from typing import get_args

    from devcake.config import AppConfig

    data = build()
    assert data["continuation_policies"] == sorted(
        get_args(AppConfig.model_fields["continuation_policy"].annotation))
    assert data["attempt_reset_policies"] == sorted(
        get_args(AppConfig.model_fields["attempt_reset"].annotation))


def test_mission_stages_match_mission_type_enum():
    """CAKE-88: mission-stage vocabulary from MissionType, not a hand list."""
    from devcake.domain.model import MissionType

    data = build()
    assert data["mission_stages"] == [m.value for m in MissionType]


def test_cron_id_re_matches_config():
    """CAKE-88: cron-id regex pin matches config._CRON_ID_RE (sibling of
    instance_name_re / harness_var_pattern)."""
    from devcake.config import _CRON_ID_RE

    data = build()
    assert data["cron_id_re"] == _CRON_ID_RE


def test_connection_fields_match_secrets_allowlist():
    """CAKE-88: connection field vocabulary equals secrets.CONNECTION_FIELDS
    (sorted per scope for stable JSON)."""
    from devcake.secrets import CONNECTION_FIELDS

    data = build()
    expected = {scope: sorted(fields)
                for scope, fields in CONNECTION_FIELDS.items()}
    assert data["connection_fields"] == expected
    assert set(data["connection_fields"]) == set(CONNECTION_FIELDS)


def test_action_preconditions_cover_action_specs_plus_force_freshness():
    """CAKE-88: action_preconditions covers ACTION_SPECS ∪ {force_freshness}.
    force_freshness is NOT in ACTION_SPECS — it is a separate handler that
    409s without DEVCAKE-MERGE; the pin must still carry it for the SPA menu."""
    from devcake.api.mission_actions import ACTION_SPECS
    from devcake.domain.model import LABEL_MERGE

    data = build()
    pre = data["action_preconditions"]
    assert set(pre) == set(ACTION_SPECS) | {"force_freshness"}
    for action, spec in ACTION_SPECS.items():
        assert pre[action]["require_present"] == sorted(spec.require_present)
        assert pre[action]["require_absent"] == sorted(spec.require_absent)
    assert pre["force_freshness"] == {
        "require_present": [LABEL_MERGE],
        "require_absent": [],
    }
