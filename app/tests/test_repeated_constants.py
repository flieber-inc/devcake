"""ADR-0034 canaries for CAKE-87 — one home per repeated constant.

Each family: authority next to the domain owner; consumers import/derive;
a ratchet turns red if a hand copy returns.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import get_args

import pytest

APP = Path(__file__).resolve().parents[1] / "devcake"
# Local checkout: …/devcake/scripts; app-test container: /srv/repo-scripts.
_SCRIPTS_CANDIDATES = (
    Path(__file__).resolve().parents[2] / "scripts",
    Path("/srv/repo-scripts"),
)
SCRIPTS = next((p for p in _SCRIPTS_CANDIDATES if p.is_dir()), None)


# ── Family 1: terminal RunState set ─────────────────────────────────────────

_TERMINAL_FOUR = frozenset({"finished", "failed", "timed_out", "orphaned"})


def test_terminal_states_equals_runstate_terminal_subset():
    from devcake.domain.run import RunState, TERMINAL_STATES

    all_states = frozenset(get_args(RunState))
    # Independent vocabulary: the four strings the repaired CAKE-73 path uses.
    assert TERMINAL_STATES == _TERMINAL_FOUR
    assert TERMINAL_STATES <= all_states
    assert TERMINAL_STATES == (all_states - {"dispatched", "running", "finalizing"})


def test_terminal_states_no_second_copy_in_app_package():
    """AST: only domain/run.py may define or spell the four-state set."""
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP)
        if rel.as_posix() == "domain/run.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "TERMINAL_STATES":
                        offenders.append(f"{rel}:{node.lineno} assign")
                if _is_terminal_four_literal(node.value):
                    offenders.append(f"{rel}:{node.lineno} four-literal assign")
            elif isinstance(node, ast.AnnAssign):
                if (isinstance(node.target, ast.Name)
                        and node.target.id == "TERMINAL_STATES"):
                    offenders.append(f"{rel}:{node.lineno} ann-assign")
                if node.value is not None and _is_terminal_four_literal(node.value):
                    offenders.append(f"{rel}:{node.lineno} four-literal ann-assign")
            elif (isinstance(node, ast.Compare)
                  and any(isinstance(op, ast.In) for op in node.ops)):
                for comp in node.comparators:
                    if _is_terminal_four_literal(comp):
                        offenders.append(f"{rel}:{node.lineno} `in` four-literal")
    assert not offenders, (
        "second TERMINAL_STATES / four-state literal outside domain/run.py: "
        + "; ".join(offenders)
    )


def _is_terminal_four_literal(node: ast.AST) -> bool:
    """True when ``node`` is a set/frozenset/tuple/list of exactly the four
    terminal state string constants (any order)."""
    elts: list[ast.AST] | None = None
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        elts = list(node.elts)
    elif (isinstance(node, ast.Call)
          and isinstance(node.func, ast.Name)
          and node.func.id in {"frozenset", "set"}
          and node.args):
        arg0 = node.args[0]
        if isinstance(arg0, (ast.Set, ast.Tuple, ast.List)):
            elts = list(arg0.elts)
    if elts is None or len(elts) != 4:
        return False
    vals: set[str] = set()
    for e in elts:
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            vals.add(e.value)
        else:
            return False
    return vals == _TERMINAL_FOUR


def test_spa_terminal_states_pinned_to_python():
    """spa-contracts.run_terminal_states deep-equals the Python frozenset."""
    from tests.gen_spa_contracts import build
    from devcake.domain.run import TERMINAL_STATES

    data = build()
    assert set(data["run_terminal_states"]) == set(TERMINAL_STATES)
    assert set(data["run_stopped_states"]) == set(TERMINAL_STATES) | {"finalizing"}


# ── Family 2: CLI-version semver regex ──────────────────────────────────────

_SEMVER_LITERAL = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.]+)?"


def test_cli_version_semver_authority_and_shared_import():
    """versions.CLI_VERSION_SEMVER is the one pattern; consumers share it."""
    from devcake.versions import CLI_VERSION_SEMVER, CLI_VERSION_SEMVER_RE
    from devcake import keep_set
    from devcake import config as cfg

    assert CLI_VERSION_SEMVER == _SEMVER_LITERAL
    assert CLI_VERSION_SEMVER_RE.pattern == _SEMVER_LITERAL
    assert keep_set._SEMVER is CLI_VERSION_SEMVER_RE
    assert CLI_VERSION_SEMVER_RE.fullmatch("2.1.250")
    assert not CLI_VERSION_SEMVER_RE.fullmatch("latest")
    assert not CLI_VERSION_SEMVER_RE.fullmatch("2.1")
    with pytest.raises(ValueError):
        cfg.DevType(name="x", harness_template="claude-code", cli_version="latest")
    with pytest.raises(ValueError):
        cfg.DevType(name="x", harness_template="claude-code", cli_version="2.1")
    cfg.DevType(name="x", harness_template="claude-code", cli_version="2.1.250")


def test_cli_version_semver_no_raw_literal_outside_versions():
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP)
        if rel.as_posix() == "versions.py":
            continue
        if _SEMVER_LITERAL in path.read_text():
            offenders.append(str(rel))
    assert SCRIPTS is not None, (
        "scripts/ missing — bind scripts → /srv/repo-scripts")
    factory = SCRIPTS / "dev_factory" / "core.py"
    assert _SEMVER_LITERAL not in factory.read_text(), (
        "scripts/dev_factory/core.py must import CLI_VERSION_SEMVER_RE, "
        "not re-spell the regex")
    assert not offenders, (
        "raw CLI semver regex literal outside versions.py: "
        + ", ".join(offenders)
    )


# ── Family 3: skill / instance name shapes ──────────────────────────────────

def test_skill_name_shapes_lockstep():
    """SKILL_NAME_RE + INSTANCE_NAME_BODY compose the DevType.skills pattern."""
    from devcake.config import (INSTANCE_NAME_BODY, _INSTANCE_NAME_RE,
                                DevType, skill_ref_pattern)
    from devcake.domain.skills import SKILL_NAME_RE

    assert _INSTANCE_NAME_RE == rf"^{INSTANCE_NAME_BODY}$"
    composed = skill_ref_pattern()
    assert composed == rf"(?:{INSTANCE_NAME_BODY}/)?{SKILL_NAME_RE}"

    # Independent expected values — not recomputed from production helpers.
    good_local = ["tdd", "pr-hygiene", "write_spec2"]
    good_external = ["src/my-skill", "houseskills/tdd"]
    bad = ["UPPER", "-lead", "a" * 65, "1x/skill", "has-hyphen/skill",
           "a/b/c", "SL/ash", "/ash", "sl/"]

    for name in good_local + good_external:
        assert re.fullmatch(composed, name), name
        DevType(name="senior-dev", harness_template="claude-code", skills=[name])

    for name in bad:
        assert not re.fullmatch(composed, name), name
        with pytest.raises(ValueError):
            DevType(name="senior-dev", harness_template="claude-code",
                    skills=[name])

    assert re.fullmatch(SKILL_NAME_RE, "tdd")
    assert not re.fullmatch(SKILL_NAME_RE, "UPPER")
    assert not re.fullmatch(SKILL_NAME_RE, "src/tdd")


def test_instance_name_body_drives_repo_marker_and_skills_source():
    from devcake.config import INSTANCE_NAME_BODY
    from devcake.domain.orchestrator.markers import REPO_MARKER
    from devcake.domain.skills import SKILL_NAME_RE
    import re as _re

    assert INSTANCE_NAME_BODY in REPO_MARKER.pattern
    # skills._get_external source half must use INSTANCE_NAME_BODY (no re-spell).
    skills_src = (APP / "domain" / "skills.py").read_text()
    assert 'r"[a-z][a-z0-9]{0,11}"' not in skills_src
    assert "INSTANCE_NAME_BODY" in skills_src
    # repo_mirror must use SKILL_NAME_RE, not a local re-spell.
    mirror_src = (APP / "domain" / "repo_mirror.py").read_text()
    assert 'r"[a-z0-9][a-z0-9_-]{0,63}$"' not in mirror_src
    assert "SKILL_NAME_RE" in mirror_src
    _ = SKILL_NAME_RE, _re  # imported for clarity of public-seam intent


# ── Family 4: redaction registration keys ───────────────────────────────────

def test_redaction_key_builders_formats():
    from devcake import secrets as s

    assert s.conn_redact_key("repo", "main", "token") == "conn:repo:main:token"
    assert s.harness_redact_key("XAI_API_KEY") == "harness:XAI_API_KEY"
    assert s.cred_redact_key("main-dev", "grok-auth.json") == (
        "cred:main-dev:grok-auth.json")


def test_redaction_key_fstrings_only_inside_builders():
    """Raw f\"conn:\" / f\"harness:\" / f\"cred:\" only inside the three builders."""
    path = APP / "secrets.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    builders = {"conn_redact_key", "harness_redact_key", "cred_redact_key"}
    builder_linenos: set[int] = set()
    for n in tree.body:
        if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name in builders):
            for node in ast.walk(n):
                if hasattr(node, "lineno"):
                    builder_linenos.add(node.lineno)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for v in node.values:
            if (isinstance(v, ast.Constant) and isinstance(v.value, str)
                    and v.value.startswith(("conn:", "harness:", "cred:"))):
                if node.lineno not in builder_linenos:
                    offenders.append(f"line {node.lineno}")
                break
    assert not offenders, (
        "redaction key f-strings outside builders: " + "; ".join(offenders)
    )


# ── Family 5: work/reference disjointness chokepoint ────────────────────────

_WR_NEEDLE = "cannot be both a work repo and a reference repo"


def test_work_reference_disjointness_single_chokepoint():
    """Error text for work∩reference lives only in validate_memory_bindings."""
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP)
        text = path.read_text()
        if _WR_NEEDLE not in text:
            continue
        if rel.as_posix() != "config.py":
            offenders.append(str(rel))
            continue
        tree = ast.parse(text, filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Constant)
                        and isinstance(sub.value, str)
                        and _WR_NEEDLE in sub.value):
                    if node.name != "validate_memory_bindings":
                        offenders.append(f"config.py:{node.name}")
    from devcake.config import AppConfig, PMOInstance, RepoInstance

    with pytest.raises(ValueError, match=re.escape(_WR_NEEDLE)):
        AppConfig(
            pmos=[PMOInstance(name="linear", team_key="T",
                              repos=["web"], reference_repos=["web"])],
            repos=[RepoInstance(name="web", url="https://h/o/r")],
        )
    assert not offenders, (
        "work∩reference error text outside validate_memory_bindings: "
        + ", ".join(offenders)
    )
