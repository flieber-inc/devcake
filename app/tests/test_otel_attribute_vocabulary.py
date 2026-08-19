"""One OTel attribute vocabulary — pin docs / emitters / OO SQL / SPA (CAKE-86).

``devcake.telemetry.attributes`` is the authoritative registry (ADR-0034).
This suite is the pinned mirror: drift between the five surfaces turns red.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from devcake.telemetry.attributes import (
    ATTRIBUTES,
    COST_HOME_SPAN,
    PMO_TRANSIENT_SPAN,
    TOKEN_ATTRS,
    oo_field,
)

# Host checkout: repo/app/tests → repo/… ; container binds under /srv/…
_REPO = Path(__file__).resolve().parents[2]
_CANDIDATES = {
    "docs12": [
        Path("/srv/docs/12-observability.md"),
        _REPO / "docs" / "12-observability.md",
    ],
    "docs15": [
        Path("/srv/docs/15-errors-and-retries.md"),
        _REPO / "docs" / "15-errors-and-retries.md",
    ],
    "provision": [
        Path("/srv/repo-scripts/provision_oo.py"),
        _REPO / "scripts" / "provision_oo.py",
    ],
    "runs_page": [
        Path("/srv/admin-runs-page.jsx"),
        _REPO / "admin" / "spa" / "src" / "pages" / "RunsPage.jsx",
    ],
    "app_pkg": [
        Path(__file__).resolve().parents[1] / "devcake",
    ],
    "images": [
        Path("/srv/images-tree"),
        Path("/srv/images"),
        _REPO / "images",
    ],
}


def _first_existing(key: str) -> Path:
    for p in _CANDIDATES[key]:
        if p.exists():
            return p
    return _CANDIDATES[key][-1]


def _require(key: str) -> Path:
    path = _first_existing(key)
    assert path.exists(), (
        f"missing {path} — bind the source into the pytest runner "
        f"(see scripts/pytest_app.sh / ci.yml) for {key}"
    )
    return path


def _attr_from_call_arg(node: ast.AST) -> set[str]:
    """Resolve set_attribute's first arg to registry names (literals + tokens f-string)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if node.value.startswith("devcake."):
            return {node.value}
        return set()
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if (isinstance(first, ast.Constant) and isinstance(first.value, str)
                and first.value == "devcake.tokens."):
            return set(TOKEN_ATTRS)
    return set()


def _collect_emitted(roots: list[Path]) -> set[str]:
    found: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            # Skip tests / __pycache__ if a tree is wide
            if "/tests/" in path.as_posix() or path.name.startswith("test_"):
                continue
            try:
                tree = ast.parse(path.read_text(), filename=str(path))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and func.attr == "set_attribute"):
                    continue
                if not node.args:
                    continue
                found |= _attr_from_call_arg(node.args[0])
    return found


def _docs12_section3_names(text: str) -> set[str]:
    # Prose may sit between the heading and the fenced mirror of ATTRIBUTES.
    m = re.search(
        r"## 3\. Attribute registry.*?```\n(.*?)```",
        text,
        flags=re.DOTALL,
    )
    assert m, "docs/12 §3 code fence missing — registry must stay in a fenced block"
    return set(re.findall(r"devcake\.[a-z0-9_.]+", m.group(1)))


def test_oo_field_maps_dots_to_underscores():
    assert oo_field("devcake.run.id") == "devcake_run_id"
    assert oo_field("devcake.cost.usd") == "devcake_cost_usd"
    assert oo_field("devcake.audit.action") == "devcake_audit_action"


def test_registry_drops_never_emitted_and_keeps_queryable_core():
    """Independent expected values: names that must / must not be in the set."""
    assert "devcake.mission.id" not in ATTRIBUTES
    assert "devcake.run.seq" not in ATTRIBUTES
    for name in (
        "devcake.run.id",
        "devcake.outcome",
        "devcake.cost.usd",
        "devcake.cost.usd_estimated",
        "devcake.instance",
        "devcake.audit.action",
        *TOKEN_ATTRS,
    ):
        assert name in ATTRIBUTES, name


def test_emitters_only_stamp_registry_names():
    app = _require("app_pkg")
    images = _first_existing("images")
    roots = [app]
    if images.exists():
        roots.append(images)
    emitted = _collect_emitted(roots)
    assert emitted, "AST scan found no set_attribute('devcake…') — path wrong?"
    rogue = sorted(emitted - ATTRIBUTES)
    assert not rogue, (
        "emitters stamp names absent from telemetry.attributes.ATTRIBUTES: "
        + ", ".join(rogue)
    )


def test_docs_12_section3_equals_registry():
    text = _require("docs12").read_text()
    fence = _docs12_section3_names(text)
    assert fence == ATTRIBUTES, (
        "docs/12 §3 drifted from ATTRIBUTES:\n"
        f"  only_in_docs={sorted(fence - ATTRIBUTES)}\n"
        f"  only_in_code={sorted(ATTRIBUTES - fence)}"
    )


def test_docs_12_tokens_and_cost_live_on_run_finalize_not_dev_run():
    text = _require("docs12").read_text()
    # §2 table row for dev.run must not claim tokens/cost (those ride run.finalize).
    m = re.search(r"\|\s*`dev\.run`\s*\|([^|]+)\|([^|]+)\|([^|]+)\|", text)
    assert m, "docs/12 §2 missing dev.run row"
    content = m.group(3)
    # Must not claim tokens/cost as carried attrs (negation prose is fine).
    assert "`devcake.tokens" not in content, content
    assert "`devcake.cost" not in content, content
    assert COST_HOME_SPAN == "run.finalize"
    assert "run.finalize" in text
    # Outcome dashboard narrative / panel table must not pin outcome to dev.run.
    panel = re.search(
        r"\|\s*2\s*\|\s*\*\*Dev runs by outcome[^*]*\*\*\s*\|\s*([^|]+)\|",
        text,
    )
    assert panel, "docs/12 §5 outcome panel row missing"
    assert "run.finalize" in panel.group(1), panel.group(1)
    assert "dev.run" not in panel.group(1), panel.group(1)


def test_docs_12_pmo_transient_is_on_poll_instance():
    text = _require("docs12").read_text()
    cycle = re.search(r"\|\s*`poll\.cycle`\s*\|([^|]+)\|([^|]+)\|([^|]+)\|", text)
    instance = re.search(
        r"\|\s*`poll\.instance`\s*\|([^|]+)\|([^|]+)\|([^|]+)\|", text)
    assert cycle and instance
    assert "PMO_TRANSIENT" not in cycle.group(3), cycle.group(3)
    assert "PMO_TRANSIENT" in instance.group(3), instance.group(3)
    assert PMO_TRANSIENT_SPAN == "poll.instance"


def test_pmo_transient_alert_queries_poll_instance():
    provision = _require("provision").read_text()
    docs15 = _require("docs15").read_text()
    # Script alert arm
    assert (
        f"(operation_name = '{PMO_TRANSIENT_SPAN}' AND "
        f"{oo_field('devcake.outcome')} = 'PMO_TRANSIENT')"
    ) in provision, (
        "provision_oo.py PMO_TRANSIENT arm must query poll.instance "
        "(stamp lives on the per-instance child, not the cycle)"
    )
    assert "operation_name = 'poll.cycle' AND devcake_outcome = 'PMO_TRANSIENT'" not in provision
    # docs/15 §6 table
    row = re.search(
        r"\|\s*`devcake-pmo-forge-transient`\s*\|\s*([^|]+)\|",
        docs15,
    )
    assert row, "docs/15 §6 missing pmo-forge-transient row"
    assert "poll.instance" in row.group(1), row.group(1)
    assert "poll.cycle" not in row.group(1), row.group(1)


def test_cost_and_outcome_sql_constrain_to_run_finalize():
    provision = _require("provision").read_text()
    cost_col = oo_field("devcake.cost.usd")
    est_col = oo_field("devcake.cost.usd_estimated")
    outcome_col = oo_field("devcake.outcome")

    # Outcome panel — mission vocabulary lives on run.finalize, not dev.run
    assert f"operation_name = '{COST_HOME_SPAN}'" in provision
    assert "operation_name = 'dev.run'" not in provision

    # Cost panels — string literals may split across lines; require both
    # the home-span filter and the >0 guard next to each SUM.
    assert (
        f"WHERE operation_name = '{COST_HOME_SPAN}' AND {cost_col} > 0"
        in provision
    ), "billed cost panel must filter run.finalize + cost > 0"
    assert re.search(
        rf"SUM\({re.escape(est_col)}\).*?operation_name = '{COST_HOME_SPAN}'"
        rf".*?{re.escape(est_col)} > 0",
        provision,
        flags=re.DOTALL,
    ), "estimated cost panel must filter run.finalize"

    # Daily cost alert — SQL is split across adjacent string literals
    idx = provision.index('"devcake-daily-cost"')
    alert_blob = provision[idx:idx + 280]
    assert f"operation_name = '{COST_HOME_SPAN}'" in alert_blob, alert_blob
    assert cost_col in alert_blob
    # Outcome panel groups by outcome on finalize
    assert outcome_col in provision


def test_spa_trace_filter_uses_oo_field_for_run_id():
    src = _require("runs_page").read_text()
    expected = oo_field("devcake.run.id")
    assert expected == "devcake_run_id"
    # Concrete shape used by RunsPage.traceUrl (OO flattens dots → underscores)
    assert f"{expected}='" in src, (
        f"RunsPage.traceUrl must filter on {expected!r} "
        f"(oo_field('devcake.run.id'))"
    )
