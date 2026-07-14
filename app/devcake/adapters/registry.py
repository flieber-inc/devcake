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
    api_key_env_default: str
    secret_env_vars: list[str]
    token_patterns: list[str]           # regex sources for redaction
    secret_shape_prefixes: list[str]    # SPA paste-guard prefixes


PMO_SYSTEMS: dict[str, PMOSystemInfo] = {
    "linear": PMOSystemInfo(
        id="linear",
        display_name="Linear",
        api_key_env_default="LINEAR_API_KEY",
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
        return LinearAdapter(inst.api_key)
    raise AssertionError("unreachable")  # registry and constructors in sync


def _forge_classes() -> dict[str, type]:
    # lazy so importing the registry never drags in httpx-heavy adapters
    from .github import GitHubForge
    from .gitlab import GitLabForge
    return {"github": GitHubForge, "gitlab": GitLabForge}


def forges() -> dict[str, "ForgeDescriptor"]:
    """id → descriptor for every registered forge (SPA registry endpoint,
    redaction contributions)."""
    return {fid: cls.descriptor for fid, cls in _forge_classes().items()}


def make_forge(inst) -> "ForgePort":
    """Construct the adapter for one configured RepoInstance (config.repos[i])."""
    import os
    classes = _forge_classes()
    if inst.forge not in classes:
        raise ValueError(f"unknown forge {inst.forge!r} — registered: "
                         f"{sorted(classes)}")
    # Every forge credential env name is operator-renamable (token_env,
    # token_ro_env, reviewer_token_env), so the static redaction lists can't
    # know them — register the resolved values here (construction is the one
    # choke point every boot/reload/dry-run passes through). Over-registering
    # a value already covered by the static lists is harmless.
    from ..security import register_runtime_secret
    reviewer = os.environ.get(inst.reviewer_token_env or "") or None
    for kind, value in (("token", inst.token),
                        ("token_ro", getattr(inst, "token_ro", "")),
                        ("reviewer", reviewer or "")):
        if value:
            register_runtime_secret(f"forge_{kind}:{inst.forge}", value)
    return classes[inst.forge](inst.url, inst.token, reviewer,
                               api_base=inst.api_base)
