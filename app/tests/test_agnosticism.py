"""F1 forge-agnosticism tripwire (docs/16 M8): behavior-first assertions that
nothing forge-specific lives outside adapters/.

The primary checks are behavioral: the import graph (no vendor-adapter imports
outside the registry), defaults provably resolving through descriptors, and
warning copy derived from config — all parameterized over the registry, so a
newly registered forge is covered the day it lands. The string-constant scan
at the end is explicitly SECONDARY (comments/docstrings legitimately name
forges; only string literals in non-adapter code are suspect)."""

import ast
from pathlib import Path

import pytest

import devcake
from devcake.adapters import registry
from devcake.adapters.registry import DEFAULT_FORGE, PMO_SYSTEMS, forges
from devcake.config import AppConfig, RepoInstance

PKG_ROOT = Path(devcake.__file__).parent          # .../app/devcake


def _vendor_ids() -> set[str]:
    """Every registered vendor adapter package name — forges AND PMO systems
    (the F1 rule is symmetric: the registry is the only construction site)."""
    return set(forges()) | set(PMO_SYSTEMS)


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(PKG_ROOT.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _import_targets(path: Path) -> list[str]:
    """Absolute dotted names this file imports (relative imports resolved)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    mod_parts = _module_name(path).split(".")
    is_pkg = path.name == "__init__.py"
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                pkg = mod_parts if is_pkg else mod_parts[:-1]
                anchor = pkg[:len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                base = ".".join(anchor + ([base] if base else []))
            targets.append(base)
            # `from devcake.adapters import github` names the submodule via
            # the alias — cover that shape too
            targets.extend(f"{base}.{a.name}" for a in node.names)
    return targets


def _package_files() -> list[Path]:
    return sorted(PKG_ROOT.rglob("*.py"))


def test_no_vendor_adapter_imports_outside_registry():
    """THE tripwire: `devcake.adapters.<vendor>` may be imported only by the
    registry (and by the vendor package internally). Everything else must go
    through registry.make_pmo/make_forge/forges()."""
    forbidden = {f"devcake.adapters.{vid}" for vid in _vendor_ids()}
    offenders = []
    for path in _package_files():
        rel = path.relative_to(PKG_ROOT)
        if rel == Path("adapters/registry.py"):
            continue
        if len(rel.parts) >= 2 and rel.parts[0] == "adapters" and rel.parts[1] in _vendor_ids():
            continue                      # the vendor package itself
        for target in _import_targets(path):
            if any(target == f or target.startswith(f + ".") for f in forbidden):
                offenders.append(f"{rel}: imports {target}")
    assert not offenders, (
        "vendor adapter imports outside the registry (F1):\n" + "\n".join(offenders))


def test_config_default_forge_resolves_through_registry():
    """A reintroduced static forge-name default fails here (F1)."""
    assert RepoInstance().forge == DEFAULT_FORGE




def test_write_token_warning_fires_per_repo(tmp_path, monkeypatch):
    """The forge-write-token warning fires per configured repo with a write
    token but no RO token — its id is repo-scoped (dismissals don't bleed)."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import security
    from devcake import secrets as secrets_store
    secrets_store.write_connection_secret("repo", "main", "token", "write-tok-123")
    cfg = AppConfig(repos=[RepoInstance(name="main",
                                        url="https://host.example/o/r")])
    warns = {w["id"] for w in security.security_warnings(cfg)}
    assert "forge-write-token:main" in warns


def test_port_declares_no_forge_specific_git_identity():
    """The port must not bake in any forge's git identity; every registered
    adapter supplies its own."""
    from devcake.ports.forge import ForgeDescriptor
    assert ForgeDescriptor.model_fields["git_email"].is_required(), (
        "ForgeDescriptor.git_email regained a default — forge identity "
        "belongs in adapters, not the port (F1)")
    for fid, d in forges().items():
        assert d.git_email, f"{fid} descriptor supplies no git_email"
        assert d.pr_noun, f"{fid} descriptor supplies no pr_noun"


# ---------------------------------------------------------------------------
# SECONDARY check (docs/16 M8: "a forge-name literal grep is at most a
# secondary check with an explicit allowlist"). Only string LITERALS in
# non-adapter code are scanned; comments and docstrings are exempt by
# construction (docstrings excluded below).

_STRING_SCAN_ALLOWLIST: dict[str, list[str]] = {
    # rel-path → substrings expected there, with a reason. Empty today: 8.1
    # removed every forge-host literal outside adapters/.
}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def test_no_forge_host_literals_outside_adapters():
    hosts = ("github.com", "gitlab.com")
    offenders = []
    for path in _package_files():
        rel = path.relative_to(PKG_ROOT)
        if rel.parts[0] == "adapters":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        doc_ids = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in doc_ids:
                hits = [h for h in hosts if h in node.value]
                allowed = _STRING_SCAN_ALLOWLIST.get(str(rel), [])
                for h in hits:
                    if not any(a in node.value for a in allowed):
                        offenders.append(f"{rel}:{node.lineno}: {node.value[:60]!r}")
    assert not offenders, (
        "forge-host string literals outside adapters/ (extend the allowlist "
        "only with a reason):\n" + "\n".join(offenders))
