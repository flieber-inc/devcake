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
    registered system contributes its token shapes, configured or not.
    SPA field labels (team_key_*, needs_api_base) keep the Config form
    system-agnostic so adding an adapter never hardcodes Linear copy."""
    id: str
    display_name: str
    secret_env_vars: list[str]
    token_patterns: list[str]           # regex sources for redaction
    secret_shape_prefixes: list[str]    # SPA paste-guard prefixes
    needs_api_base: bool = False
    team_key_label: str = "Team key"
    team_key_help: str = (
        "The team's short key — the prefix of its issue IDs (PRJ for PRJ-123). "
        "This instance watches only this team. Empty = instance stays idle.")
    api_base_help: str = ""
    # ADR-0030 composer: whether create_mission honors a priority (forge-
    # issue systems normalize everything to medium — the SPA hides the
    # Priority field instead of offering a control that silently does nothing)
    supports_priority: bool = True
    # Shown in the admin PMO card (and New Mission) when this system is
    # selected. Empty = no note. Copy lives here so the SPA stays
    # system-agnostic (docs/05 §1a).
    operator_note: str = ""
    attachments_supported: bool = True
    relations_supported: bool = True


PMO_SYSTEMS: dict[str, PMOSystemInfo] = {
    "linear": PMOSystemInfo(
        id="linear",
        display_name="Linear",
        secret_env_vars=["LINEAR_API_KEY"],
        token_patterns=[r"\blin_api_[A-Za-z0-9]{20,}\b",
                        r"\blin_oauth_[A-Za-z0-9]{20,}\b"],
        secret_shape_prefixes=["lin_api_", "lin_oauth_"],
    ),
    # Separate package/id from forge `gitea` so the F1 import tripwire and
    # ForgePort stay cleanly separated (PMOPort only — docs/05 forge-issue).
    "gitea_issues": PMOSystemInfo(
        id="gitea_issues",
        display_name="Gitea Issues",
        secret_env_vars=["GITEA_TOKEN", "GITEA_SERVER_TOKEN"],
        # 40-hex tokens collide with git SHAs — value registration only
        # (same posture as the Gitea forge adapter, ADR-0010).
        token_patterns=[],
        secret_shape_prefixes=[],
        needs_api_base=True,
        team_key_label="Issues repo",
        team_key_help=(
            "owner/repo of the dedicated issues board this instance watches "
            "(e.g. devcake-pmo/missions). Not a per-mission work repo. "
            "Empty = instance stays idle."),
        api_base_help=(
            "Gitea origin reachable from the app container. Bundled stack: "
            "http://gitea:3000 (browser UI is http://localhost:3300). "
            "External: https://gitea.example.com"),
        supports_priority=False,   # forge-issue: priority always medium (§9.2)
    ),
    "github_issues": PMOSystemInfo(
        id="github_issues",
        display_name="GitHub Issues",
        secret_env_vars=["GITHUB_TOKEN", "GH_TOKEN"],
        token_patterns=[r"\bghp_[A-Za-z0-9]{20,}\b",
                        r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"],
        secret_shape_prefixes=["ghp_", "github_pat_"],
        needs_api_base=True,
        team_key_label="Issues repo",
        team_key_help=(
            "owner/repo of the dedicated issues board this instance watches "
            "(e.g. myorg/missions). Not a per-mission work repo. "
            "Empty = instance stays idle."),
        api_base_help=(
            "GitHub API origin reachable from the app container. "
            "github.com: leave empty or https://api.github.com. "
            "GitHub Enterprise: https://ghe.example.com/api/v3"),
        supports_priority=False,
        attachments_supported=False,
        relations_supported=True,
        operator_note=(
            "GitHub's public API cannot attach files to issues. Transcripts, "
            "plans, and other deliverables still land in the mission's "
            "activity repo; the ticket comment carries a short reference. "
            "This is a GitHub limitation, not a DevCake setting."
        ),
    ),
    "gitlab_issues": PMOSystemInfo(
        id="gitlab_issues",
        display_name="GitLab Issues",
        secret_env_vars=["GITLAB_TOKEN", "GITLAB_PAT"],
        token_patterns=[r"\bglpat-[A-Za-z0-9_\-]{20,}\b"],
        secret_shape_prefixes=["glpat-"],
        needs_api_base=True,
        team_key_label="Issues repo",
        team_key_help=(
            "path_with_namespace of the dedicated issues board "
            "(e.g. mygroup/missions). Not a per-mission work repo. "
            "Empty = instance stays idle."),
        api_base_help=(
            "GitLab origin reachable from the app container. "
            "gitlab.com: https://gitlab.com. Self-hosted: "
            "https://gitlab.example.com"),
        supports_priority=False,
        attachments_supported=True,
        relations_supported=False,
        operator_note=(
            "Blocked-by issue links need GitLab Premium (or self-hosted EE). "
            "DevCake probes the live token — Free boards will not write "
            "decomposition traffic-control edges, and child missions will "
            "not block each other. File attachments work. This is a GitLab "
            "license limit, not a DevCake setting."
        ),
    ),
}


def make_pmo(inst) -> PMOPort:
    """Construct the adapter for one configured PMOInstance (config.pmos[i])."""
    if inst.system not in PMO_SYSTEMS:
        raise ValueError(f"unknown PMO system {inst.system!r} — registered: "
                         f"{sorted(PMO_SYSTEMS)}")
    # Factory registration is the redaction line for unusual key shapes under
    # the 16-char disk-scan floor (store write + register_all cover the rest).
    # Every system registers under the same pmo_key:{name} scheme.
    from ..security import register_runtime_secret
    if inst.api_key:
        register_runtime_secret(f"pmo_key:{inst.name}", inst.api_key)
    if inst.system == "linear":
        from .linear import LinearAdapter
        return LinearAdapter(inst.api_key, instance=inst.name)
    if inst.system == "gitea_issues":
        from .gitea_issues import GiteaIssuesAdapter
        return GiteaIssuesAdapter(
            inst.api_base, inst.api_key, inst.team_key, instance=inst.name)
    if inst.system == "github_issues":
        from .github_issues import GitHubIssuesAdapter
        return GitHubIssuesAdapter(
            inst.api_base, inst.api_key, inst.team_key, instance=inst.name)
    if inst.system == "gitlab_issues":
        from .gitlab_issues import GitLabIssuesAdapter
        return GitLabIssuesAdapter(
            inst.api_base, inst.api_key, inst.team_key, instance=inst.name)
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
    tokens for redaction like make_forge does.

    Keys are content-addressed (sha256 prefix), not token[:6]: Gitea PATs are
    40-hex and concurrent mission tokens can share a 6-char prefix — a
    prefix key would overwrite in _runtime_secrets and eventually drop older
    values out of the prior list (CAKE-38).
    """
    import hashlib
    from .gitea import GiteaForge
    from ..security import register_runtime_secret
    for v in (token, reviewer_token):
        if v:
            digest = hashlib.sha256(v.encode()).hexdigest()[:16]
            register_runtime_secret(f"forge_token:gitea:{digest}", v)
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
