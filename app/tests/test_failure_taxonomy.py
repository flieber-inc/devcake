"""ADR-0027 — the failure taxonomy's doctrine, made executable.

Four layers, one table (`domain/failure_taxonomy.py`):

1. Row invariants — the pairing doctrine ("uncounted ⇒ breaker"), code/class
   uniqueness, the structured-only sibling rule.
2. Derivation pins — the views the old hand-synchronized encodings became
   (UNCOUNTED_CLASSES, the reconcile regex codes, KILL_CLASSES, the brake
   evidence sets) asserted against their pre-ADR-0027 literal values, so the
   tableization provably preserved membership.
3. App/image parity — the image package DECLARES its exit contract
   (`fault.PRODUCED`); we import it through the images/common mount and
   compare real objects across the version-skew boundary. An AST honesty
   check keeps the manifest true against dev_entrypoint.py's actual exit
   sites. No text scraping of the entrypoint's logic — the H1 dialect
   restructuring must not break this file.
4. Docs parity — docs/15 §1's table names exactly the classes that exist.

Plus the structural scan: no bare `DEV_*` string constant may exist in
app/devcake outside the table module — a typo'd class string used to
silently never match; now it fails here.
"""

import ast
import re
import sys
from pathlib import Path

from devcake.domain import failure_taxonomy as ft

# host checkout: repo/app/tests → repo/…; app container: /srv/tests → /srv/…
_ROOTS = [Path(__file__).parents[2], Path(__file__).parents[1]]
IMAGES_COMMON = next(
    (r / "images" / "common" for r in _ROOTS
     if (r / "images" / "common" / "devcake_dev").is_dir()),
    _ROOTS[0] / "images" / "common")
DOCS_15 = next(
    (r / "docs" / "15-errors-and-retries.md" for r in _ROOTS
     if (r / "docs" / "15-errors-and-retries.md").exists()),
    _ROOTS[0] / "docs" / "15-errors-and-retries.md")
APP_PKG = Path(__file__).parents[1] / "devcake"

MOUNT_HINT = ("mount missing — the pytest runner must bind {src} "
              "(see scripts/pytest_app.sh / ci.yml)")

CONTAINER_ROWS = [r for r in ft.TABLE if r.exit_codes]
APP_SIDE_ROWS = [r for r in ft.TABLE if not r.exit_codes]


# ── 1. row invariants ────────────────────────────────────────────────────────

def test_uncounted_rows_always_latch_a_breaker():
    """The dispatch.py pairing doctrine, previously a comment: "never counts"
    is livelock-safe ONLY because the same row halts dispatch entirely."""
    for row in ft.TABLE:
        if row.counting == "never":
            assert row.breaker in ("dev_type", "repo"), (
                f"{row.error_class} is uncounted but latches no breaker — "
                "it would be re-dispatched forever (the DEV_FORGE lesson)")


def test_bounded_rows_never_latch_and_never_exempt():
    """excusable / forge-bounded rows are the LEDGER-bounded arm: excusals_left
    bounds them per (mission, mission_type, class). A breaker or an
    UNCOUNTED_CLASSES membership on the same row would bypass that bound."""
    for row in ft.TABLE:
        if row.counting in ("excusable", "forge-bounded"):
            assert row.breaker is None
            assert row.error_class not in ft.UNCOUNTED_CLASSES


def test_excusable_rows_are_brake_visible():
    """An 'excusable' row's evidence must feed the brake (that correlation IS
    the excusal predicate); forge-bounded's bound is the excusal ledger alone."""
    for row in ft.TABLE:
        if row.counting == "excusable":
            assert row.brake_evidence in ("always", "opt-in")
        else:
            assert row.brake_evidence == "never"


def test_class_strings_and_kill_states_are_unique():
    assert len(ft.BY_CLASS) == len(ft.TABLE)
    kill_states = [r.kill_state for r in ft.TABLE if r.kill_state]
    assert len(kill_states) == len(set(kill_states))


def test_exit_codes_are_unique_up_to_the_structured_pair():
    """A code owned by two rows would make classification ambiguous. The one
    legal share: a structured_only row rides its bare sibling's code (13 —
    DEV_FORGE_AUTH over DEV_FORGE) and is selected only by the container's
    own structured classification."""
    for code in {c for r in CONTAINER_ROWS for c in r.exit_codes}:
        owners = [r for r in CONTAINER_ROWS if code in r.exit_codes]
        bare = [r for r in owners if not r.structured_only]
        assert len(bare) == 1, f"exit {code} has {len(bare)} bare owners"


def test_structured_only_rows_have_a_bare_sibling():
    """Without a bare sibling on the same code, a pre-taxonomy image (no
    structured class) would make that exit code unclassifiable."""
    for row in CONTAINER_ROWS:
        if row.structured_only:
            assert any(code in ft.BY_EXIT_CODE for code in row.exit_codes)


def test_excusal_precondition_only_where_excusals_exist():
    """excusal_requires_structured_class is exit 15's skew rule; on a row the
    brake never sees, the flag would be dead — flag it as a table bug."""
    for row in ft.TABLE:
        if row.excusal_requires_structured_class:
            assert row.counting == "excusable"


def test_app_side_rows_are_kill_or_operator_paths():
    for row in APP_SIDE_ROWS:
        assert not row.orphan_recoverable  # nothing to recover from stderr
        assert not row.structured_only
        assert row.kill_state or row.error_class == ft.DEV_OPERATOR_STOP


def test_classify_resolves_the_structured_pair():
    assert ft.classify(13, "").error_class == ft.DEV_FORGE
    assert ft.classify(13, ft.DEV_FORGE_AUTH).error_class == ft.DEV_FORGE_AUTH
    # a MISMATCHED structured class falls to the bare row for the code —
    # wording/skew can never stamp the breaker-latching class
    assert ft.classify(13, ft.DEV_HARNESS_FAULT).error_class == ft.DEV_FORGE
    assert ft.classify(15, ft.DEV_FORGE_AUTH).error_class == ft.DEV_HARNESS_FAULT
    assert ft.classify(17, "") is None          # emitted by nothing
    assert ft.classify(None, "") is None        # payload without exit_code


# ── 2. derivation pins (pre-ADR-0027 literal values) ─────────────────────────

def test_uncounted_classes_membership_is_preserved():
    assert ft.UNCOUNTED_CLASSES == frozenset({ft.DEV_AUTH, ft.DEV_FORGE_AUTH})


def test_orphan_recoverable_codes_match_the_old_regex():
    """reconcile's hand-written `exit status (10|11|13|14|15|16|20)` — 12
    deliberately absent (a stale orphan post-mortem must never trip the
    dev-type auth breaker from reconcile)."""
    assert ft.ORPHAN_RECOVERABLE_EXIT_CODES == (10, 11, 13, 14, 15, 16, 20)
    assert 12 not in ft.ORPHAN_RECOVERABLE_EXIT_CODES


def test_kill_classes_membership_is_preserved():
    assert ft.KILL_CLASSES == {"timed_out": ft.DEV_TIMEOUT,
                               "orphaned": ft.DEV_ORPHANED,
                               "failed": ft.DEV_KILLED}


def test_brake_evidence_sets_match_adr_0026():
    assert ft.BRAKE_ALWAYS_CLASSES == frozenset({ft.DEV_HARNESS_FAULT})
    assert ft.BRAKE_OPT_IN_CLASSES == frozenset({ft.DEV_BAD_OUTPUT})


def test_backend_health_derives_from_the_table():
    from devcake.domain import backend_health
    assert backend_health.fault_classes(False) == frozenset(
        {ft.DEV_HARNESS_FAULT})
    assert backend_health.fault_classes(True) == frozenset(
        {ft.DEV_HARNESS_FAULT, ft.DEV_BAD_OUTPUT})


# ── 3. app/image parity ──────────────────────────────────────────────────────

def _image_fault():
    assert (IMAGES_COMMON / "devcake_dev").is_dir(), MOUNT_HINT.format(
        src="images/common → /srv/images/common")
    if str(IMAGES_COMMON) not in sys.path:
        sys.path.insert(0, str(IMAGES_COMMON))
    from devcake_dev.domain import fault
    return fault


def test_image_manifest_equals_the_table():
    """The version-skew tripwire: the image package's self-declared exit
    contract must be exactly the table's container-produced surface. Real
    objects on both sides — no scraping, so the H1 dialect restructuring
    only has to keep PRODUCED true."""
    assert _image_fault().PRODUCED == ft.CONTAINER_PRODUCED


def _const_exit_codes(tree: ast.AST) -> set[int]:
    codes = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "exit"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sys"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, int)):
            codes.add(node.args[0].value)
    return codes


def _dev_class_constants(tree: ast.AST) -> set[str]:
    return {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and re.fullmatch(r"DEV_[A-Z_]+", node.value)}


def test_image_manifest_honesty():
    """PRODUCED stays true: every constant sys.exit() site and every DEV_*
    class literal in the image package is covered by the manifest. Artifact-less
    bare exits (entrypoint oauth-login 12, unknown-phase 20, and
    bus.request_reply runspec-error / timeout exit-20) are declared in
    BARE_EXIT_CODES and ride codes the manifest already lists."""
    fault = _image_fault()
    entrypoint = ast.parse((IMAGES_COMMON / "dev_entrypoint.py").read_text())
    bus = ast.parse(
        (IMAGES_COMMON / "devcake_dev" / "adapters" / "bus.py").read_text())
    produced_codes = {c for c, _ in fault.PRODUCED}
    produced_classes = {cls for _, cls in fault.PRODUCED}
    assert fault.BARE_EXIT_CODES <= produced_codes
    exits = (_const_exit_codes(entrypoint) | _const_exit_codes(bus)) - {0}
    assert exits <= produced_codes, (
        f"image exits {sorted(exits - produced_codes)} missing from "
        "fault.PRODUCED — extend the manifest AND the app-side table")
    classes = set()
    for rel in ("dev_entrypoint.py", "devcake_dev/domain/fault.py",
                "devcake_dev/workspace/clone.py"):
        classes |= _dev_class_constants(
            ast.parse((IMAGES_COMMON / rel).read_text()))
    assert classes <= produced_classes, (
        f"image classes {sorted(classes - produced_classes)} missing from "
        "fault.PRODUCED")


# ── 4. docs parity ───────────────────────────────────────────────────────────

def test_docs_15_error_table_names_exactly_the_classes():
    """Set comparison over a regex extract of §1's region — never parse
    markdown structure; prose edits must not false-fail this."""
    assert DOCS_15.exists(), MOUNT_HINT.format(src="docs → /srv/docs")
    text = DOCS_15.read_text()
    section = text.split("## 1. Error classes", 1)[1].split("\n## 2.", 1)[0]
    documented = set(re.findall(r"\bDEV_[A-Z_]+\b", section))
    assert documented == set(ft.BY_CLASS), (
        f"docs/15 §1 vs table — undocumented: "
        f"{sorted(set(ft.BY_CLASS) - documented)}; "
        f"stale: {sorted(documented - set(ft.BY_CLASS))}")


# ── structural scan: the table is the only source of class literals ──────────

def test_no_bare_dev_class_literals_outside_the_table():
    """Pre-ADR-0027, a typo'd class string silently never matched. Now every
    stamp/match imports the table's constants; an exact DEV_* string constant
    anywhere else in app/devcake fails here. (Tests and images/ are exempt —
    the image package is a separate deploy unit, bridged by the parity test.)"""
    offenders = []
    for path in sorted(APP_PKG.rglob("*.py")):
        if path.name == "failure_taxonomy.py":
            continue
        found = _dev_class_constants(ast.parse(path.read_text()))
        if found:
            offenders.append(f"{path.relative_to(APP_PKG)}: {sorted(found)}")
    assert not offenders, (
        "bare DEV_* literals (import failure_taxonomy constants instead):\n"
        + "\n".join(offenders))
