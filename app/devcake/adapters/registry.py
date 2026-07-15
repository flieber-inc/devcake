"""Adapter registry: the single place that knows which PMO systems (and, as
of the forge stage, which forges) exist and how to construct them. The domain
never imports this — api/main.py builds adapters here and injects them
(docs/01 §3). Adding a PMO system = one adapter package + one entry below."""

from pydantic import BaseModel

from ..ports.forge import ForgeDescriptor, ForgePort
from ..ports.pmo import PMOPort


class PMOSystemInfo(BaseModel):
    """Registry metadata for one PMO system. The secret_* fields feed
    security.redact (docs/14 §5) and the admin SPA's paste guard — every
    registered system contributes its token shapes, configured or not."""
    id: str
    display_name: str
    secret_env_vars: list[str]
    token_patterns: list[str]           # regex sources for redaction
    secret_shape_prefixes: list[str]    # SPA paste-guard prefixes


PMO_SYSTEMS: dict[str, PMOSystemInfo] = {
    "linear": PMOSystemInfo(
        id="linear",
        display_name="Linear",
        secret_env_vars=["LINEAR_API_KEY"],
        token_patterns=[r"\blin_api_[A-Za-z0-9]{20,}\b",
                        r"\blin_oauth_[A-Za-z0-9]{20,}\b"],
        secret_shape_prefixes=["lin_api_", "lin_oauth_"],
    ),
}


def make_pmo(inst) -> PMOPort:
    """Construct the adapter for one configured PMOInstance (config.pmos[i])."""
    if inst.system not in PMO_SYSTEMS:
        raise ValueError(f"unknown PMO system {inst.system!r} — registered: "
                         f"{sorted(PMO_SYSTEMS)}")
    if inst.system == "linear":
        from .linear import LinearAdapter
        return LinearAdapter(inst.api_key, instance=inst.name)
    raise AssertionError("unreachable")  # registry and constructors in sync


# The seed default for a first-boot RepoInstance. This is the ONE place a
# forge-name literal may act as a default — it lives inside adapters/, where
# forge knowledge belongs; config derives from it lazily (F1, docs/16 M8).
DEFAULT_FORGE = "github"


def _forge_classes() -> dict[str, type]:
    # lazy so importing the registry never drags in httpx-heavy adapters
    from .gitea import GiteaForge
    from .github import GitHubForge
    from .gitlab import GitLabForge
    return {"github": GitHubForge, "gitlab": GitLabForge, "gitea": GiteaForge}


def forges() -> dict[str, "ForgeDescriptor"]:
    """id → descriptor for every registered forge (SPA registry endpoint,
    redaction contributions)."""
    return {fid: cls.descriptor for fid, cls in _forge_classes().items()}


def make_internal_forge():
    """The bundled-Gitea provisioner (docs/16 M11). Sole construction site —
    keeps the F1 import tripwire honest (adapters.gitea imported only here)."""
    from .gitea.provision import GiteaProvisioner
    return GiteaProvisioner()


def make_gitea_adapter(url: str, token: str, reviewer_token: str | None = None):
    """Construct a Gitea ForgePort with EXPLICIT tokens (not env-resolved) —
    the app-side adapter for an internal-forge mission repo. Registers the
    tokens for redaction like make_forge does."""
    from .gitea import GiteaForge
    from ..security import register_runtime_secret
    for v in (token, reviewer_token):
        if v:
            register_runtime_secret(f"forge_token:gitea:{v[:6]}", v)
    return GiteaForge(url, token, reviewer_token)


def make_forge(inst) -> "ForgePort":
    """Construct the adapter for one configured RepoInstance (config.repos[i])."""
    import os
    classes = _forge_classes()
    if inst.forge not in classes:
        raise ValueError(f"unknown forge {inst.forge!r} — registered: "
                         f"{sorted(classes)}")
    # Credential VALUES are GUI-stored (schema v4, F5): the RepoInstance
    # properties read them by instance name. Register for redaction here —
    # construction is the one choke point every boot/reload/dry-run passes.
    from ..security import register_runtime_secret
    reviewer = inst.reviewer_token or None
    for kind, value in (("token", inst.token),
                        ("token_ro", inst.token_ro),
                        ("reviewer", reviewer or "")):
        if value:
            register_runtime_secret(f"forge_{kind}:{inst.name}", value)
    # reference-only repos (founder decision 2026-07-15) store no write token:
    # build their adapter on the read token so health probes and reads work —
    # write calls would 403, but routing never targets them with work
    return classes[inst.forge](inst.url, inst.token or inst.token_ro, reviewer,
                               api_base=inst.api_base)
