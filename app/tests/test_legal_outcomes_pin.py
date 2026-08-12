"""App LEGAL_OUTCOMES ↔ entrypoint first-line table (ADR-0034 pin).

STEWARD is deliberately NOT in the app park table (docs/03 §6): steward
illegal outcomes fail the run, they do not park a host mission. The
entrypoint still needs the row for its first-line check.
"""

import ast
from pathlib import Path

from devcake.domain.orchestrator.markers import LEGAL_OUTCOMES

ENTRYPOINT = Path("/srv/images/common/dev_entrypoint.py")
if not ENTRYPOINT.exists():
    ENTRYPOINT = (Path(__file__).resolve().parents[2]
                  / "images" / "common" / "dev_entrypoint.py")


def _entrypoint_legal_outcomes() -> dict[str, set[str]]:
    tree = ast.parse(ENTRYPOINT.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "legal_outcomes"):
            continue
        assert isinstance(node.value, ast.Dict)
        out: dict[str, set[str]] = {}
        for k, v in zip(node.value.keys, node.value.values):
            assert isinstance(k, ast.Constant)
            assert isinstance(v, ast.Set)
            out[k.value] = {elt.value for elt in v.elts
                            if isinstance(elt, ast.Constant)}
        return out
    raise AssertionError("legal_outcomes dict not found in dev_entrypoint.py")


def test_mission_rows_match_and_steward_is_entrypoint_only():
    entry = _entrypoint_legal_outcomes()
    for mt, outcomes in LEGAL_OUTCOMES.items():
        assert entry[mt] == set(outcomes), (
            f"entrypoint {mt} drifted from markers.LEGAL_OUTCOMES")
    assert entry["STEWARD"] == {"relations_mapped"}
    assert "STEWARD" not in LEGAL_OUTCOMES
