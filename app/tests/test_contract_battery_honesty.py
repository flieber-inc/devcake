"""Hermetic honesty gates for the live forge/PMO contract batteries (CAKE-83).

The scripts are stdin-fed (`python - < scripts/…`) so they cannot be imported
without running `main()`. Pure grading helpers + pinned EXPECTED_ROWS are
AST-extracted the same way `test_hello_bus_contract.py` pins hello↔bus.
"""

from __future__ import annotations

import ast
from pathlib import Path

_HERE = Path(__file__).resolve()
_SCRIPT_ROOTS = [
    _HERE.parents[2] / "scripts",
    Path("/srv/repo-scripts"),
]
_SCRIPTS = next((p for p in _SCRIPT_ROOTS if p.is_dir()), None)

# Documented check-id sets (plan lock). Counts are independent literals —
# not recomputed from the scripts' check() call sites.
EXPECTED_FORGE_ROWS = 14  # CAKE-181 adds apply_default_branch_protection round-trip
EXPECTED_PMO_ROWS = 14  # 1,2,3,4,5,5b,8,9,10,11,12,13,14,15


def _script(name: str) -> Path:
    assert _SCRIPTS is not None, (
        "scripts/ missing — bind scripts → /srv/repo-scripts "
        "(see scripts/pytest_app.sh / ci.yml)")
    path = _SCRIPTS / name
    assert path.is_file(), f"missing {path}"
    return path


def _extract(path: Path) -> dict:
    tree = ast.parse(path.read_text())
    keep: list[ast.stmt] = []
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "EXPECTED_ROWS"
                        for t in node.targets)):
            keep.append(node)
        elif (isinstance(node, ast.FunctionDef)
                and node.name == "grade_contract_battery"):
            keep.append(node)
    assert keep, (
        f"{path.name} must define EXPECTED_ROWS and grade_contract_battery "
        "so the battery cannot self-grade from len(results) alone")
    ns: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]),  # noqa: S102 — repo-controlled source
                 str(path), "exec"), ns)
    return ns


def test_forge_expected_rows_pinned_at_14():
    ns = _extract(_script("contract_tests_forge.py"))
    assert ns["EXPECTED_ROWS"] == EXPECTED_FORGE_ROWS


def test_pmo_expected_rows_pinned_at_14():
    ns = _extract(_script("contract_tests_pmo.py"))
    assert ns["EXPECTED_ROWS"] == EXPECTED_PMO_ROWS


def test_grader_rejects_short_all_pass_list():
    """The self-grading bug: N-1 PASS rows must not report as a green battery
    when EXPECTED_ROWS is N."""
    forge = _extract(_script("contract_tests_forge.py"))
    short = [(str(i), f"check-{i}", "PASS") for i in range(1, EXPECTED_FORGE_ROWS)]
    assert len(short) == EXPECTED_FORGE_ROWS - 1
    assert forge["grade_contract_battery"](short, EXPECTED_FORGE_ROWS) != 0


def test_grader_rejects_any_fail_even_at_full_count():
    forge = _extract(_script("contract_tests_forge.py"))
    rows = [(str(i), f"check-{i}", "PASS")
            for i in range(1, EXPECTED_FORGE_ROWS + 1)]
    rows[3] = ("4", "check-4", "FAIL — planted")
    assert forge["grade_contract_battery"](rows, EXPECTED_FORGE_ROWS) != 0


def test_grader_accepts_full_pass_list():
    forge = _extract(_script("contract_tests_forge.py"))
    rows = [(str(i), f"check-{i}", "PASS")
            for i in range(1, EXPECTED_FORGE_ROWS + 1)]
    assert forge["grade_contract_battery"](rows, EXPECTED_FORGE_ROWS) == 0


def test_pmo_grader_counts_skips_toward_expected_without_failing():
    pmo = _extract(_script("contract_tests_pmo.py"))
    rows = [
        ("1", "a", "PASS"), ("2", "b", "PASS"), ("3", "c", "PASS"),
        ("4", "d", "PASS"), ("5", "e", "PASS"), ("5b", "f", "PASS"),
        ("8", "g", "SKIP — no attachments"), ("9", "h", "PASS"),
        ("10", "i", "PASS"), ("11", "j", "PASS"), ("12", "k", "PASS"),
        ("13", "l", "SKIP — attachments_supported=False"),
        ("14", "m", "PASS"), ("15", "n", "SKIP — no project"),
    ]
    assert len(rows) == EXPECTED_PMO_ROWS
    assert pmo["grade_contract_battery"](rows, EXPECTED_PMO_ROWS) == 0


def test_forge_battery_does_not_reach_through_private_req():
    text = _script("contract_tests_forge.py").read_text()
    assert "forge._req" not in text, (
        "contract_tests_forge.py must obtain merge_commit_sha via "
        "ForgePort.pr_state(...).merge_commit_sha, not forge._req")
