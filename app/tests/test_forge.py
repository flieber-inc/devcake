"""docs/06 §5 port contract, both v0 adapters: mergeable() tri-state signal
mapping, the transient-409 merge retry, and the normalized ForgeError type
(GitLab must never leak httpx exceptions). No network — _req is stubbed."""
import asyncio

import httpx
import pytest

from devcake.adapters.github.adapter import GitHubForge
from devcake.ports.forge import ForgeError
from devcake.adapters.gitea.adapter import GiteaForge
from devcake.adapters.gitlab.adapter import GitLabForge


def gh():
    return GitHubForge("https://github.com/o/r", "tok")


def gl():
    return GitLabForge("https://gitlab.com/o/r", "tok")


def run_coro(c):
    return asyncio.get_event_loop().run_until_complete(c)


def stub_req(forge, payload):
    async def _req(method, path, **kw):
        return payload
    forge._req = _req
    return forge


# ── mergeable(): the docs/06 §5 signal table ─────────────────────────────────

@pytest.mark.parametrize("payload,expected", [
    ({"mergeable": False, "mergeable_state": "dirty"}, False),      # conflict
    ({"mergeable": True, "mergeable_state": "behind"}, False),      # stale branch
    ({"mergeable": False, "mergeable_state": "unknown"}, False),
    ({"mergeable": True, "mergeable_state": "clean"}, True),
    ({"mergeable": True, "mergeable_state": "unstable"}, True),
    ({"mergeable": True, "mergeable_state": "has_hooks"}, True),
    ({"mergeable": None, "mergeable_state": "unknown"}, None),      # computing
    ({"mergeable": True, "mergeable_state": "blocked"}, None),      # CI/approvals
    ({"mergeable": True, "mergeable_state": "draft"}, None),        # unrecognized
])
def test_github_mergeable_signal_map(payload, expected):
    assert run_coro(stub_req(gh(), payload).mergeable(8)) is expected


@pytest.mark.parametrize("payload,expected", [
    ({"detailed_merge_status": "conflict"}, False),
    ({"detailed_merge_status": "need_rebase"}, False),
    ({"detailed_merge_status": "mergeable"}, True),
    ({"detailed_merge_status": "checking"}, None),
    ({"detailed_merge_status": "unchecked"}, None),
    ({"detailed_merge_status": "ci_must_pass"}, None),
    ({"detailed_merge_status": "ci_still_running"}, None),
    ({"detailed_merge_status": "not_approved"}, None),              # unrecognized
    # legacy fallback (GitLab < 15.6: no detailed_merge_status field)
    ({"merge_status": "cannot_be_merged"}, False),
    ({"merge_status": "can_be_merged"}, True),
    ({"merge_status": "checking"}, None),
])
def test_gitlab_mergeable_signal_map(payload, expected):
    assert run_coro(stub_req(gl(), payload).mergeable(8)) is expected


# ── merge(): transient-409 retry ─────────────────────────────────────────────

@pytest.mark.parametrize("make", [gh, gl])
def test_merge_retries_transient_409_then_succeeds(make, monkeypatch):
    async def no_sleep(_):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    forge, calls = make(), []

    async def _req(method, path, **kw):
        calls.append(path)
        if len(calls) < 3:
            raise ForgeError("head modified", status=409)
    forge._req = _req
    run_coro(forge.merge(8))                    # two 409s, third attempt lands
    assert len(calls) == 3


@pytest.mark.parametrize("make", [gh, gl])
def test_merge_gives_up_after_retries_and_405_is_immediate(make, monkeypatch):
    async def no_sleep(_):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    forge, calls = make(), []

    async def always_409(method, path, **kw):
        calls.append(path)
        raise ForgeError("head modified", status=409)
    forge._req = always_409
    with pytest.raises(ForgeError):
        run_coro(forge.merge(8))
    assert len(calls) == 3                      # capped

    forge2, calls2 = make(), []

    async def conflict_405(method, path, **kw):
        calls2.append(path)
        if method == "GET":
            # not merged — redelivery probe (ISSUES #6) then re-raise merge error
            return {"number": 8, "html_url": "https://x/8", "state": "open",
                    "merged": False, "web_url": "https://x/8", "iid": 8}
        raise ForgeError("conflicts", status=405)
    forge2._req = conflict_405
    with pytest.raises(ForgeError) as e:
        run_coro(forge2.merge(8))
    # one merge attempt + one already-merged probe; no 409-style retries
    assert len(calls2) == 2 and e.value.status == 405


# ── error normalization: both adapters raise ForgeError with .status ─────────

class _FakeResp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, *a, **k):
        return _FakeResp(405, "cannot be merged")


@pytest.mark.parametrize("make", [gh, gl])
def test_req_raises_forge_error_with_status(make, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    with pytest.raises(ForgeError) as e:
        run_coro(make()._req("PUT", "/x"))
    assert e.value.status == 405
    assert "405" in str(e.value)


# ── ForgePort conformance + DTO shape parity across adapters ─────────────────

import inspect

from devcake.ports.forge import (BRANCH_PREFIX, BranchProtection, ForgePort,
                                 ForgeHealth, PullRequest, mission_branch)

PORT_METHODS = [n for n, v in vars(ForgePort).items()
                if callable(v) and not n.startswith("_")]


def _params(fn):
    return [p for p in inspect.signature(fn).parameters if p != "self"]


# docs/06 §1a table — honest divergence model; pin exact values so a fourth
# forge or a silent flip fails here instead of call sites guessing.
_CAPABILITIES_MATRIX = {
    "github": dict(mergeable_tristate=True, self_approval_blocked=True,
                   branch_protection_read="admin", pr_list_head_filter=True),
    "gitlab": dict(mergeable_tristate=True, self_approval_blocked=False,
                   branch_protection_read="maintainer", pr_list_head_filter=True),
    "gitea": dict(mergeable_tristate=False, self_approval_blocked=True,
                  branch_protection_read="admin", pr_list_head_filter=False),
}


@pytest.mark.parametrize("cls", [GitHubForge, GitLabForge, GiteaForge])
def test_adapters_declare_capabilities(cls):
    from devcake.ports.forge import ForgeCapabilities
    assert isinstance(cls.capabilities, ForgeCapabilities)
    expected = _CAPABILITIES_MATRIX[cls.descriptor.id]
    for field, want in expected.items():
        got = getattr(cls.capabilities, field)
        assert got == want, (
            f"{cls.__name__}.capabilities.{field}: got {got!r}, want {want!r}")


def test_capabilities_matrix_covers_every_registered_forge():
    """A newly registered forge must declare an exact §1a row here — no silent
    default ForgeCapabilities() that looks like GitHub."""
    from devcake.adapters.registry import forges
    assert set(forges()) == set(_CAPABILITIES_MATRIX)


@pytest.mark.parametrize("cls", [GitHubForge, GitLabForge, GiteaForge])
def test_adapters_implement_full_port(cls):
    for name in PORT_METHODS:
        impl = getattr(cls, name, None)
        assert impl is not None, f"{cls.__name__} missing port method {name}"
        assert _params(impl) == _params(getattr(ForgePort, name)), \
            f"{cls.__name__}.{name} signature drifted from ForgePort"


def test_mission_branch_single_definition():
    assert BRANCH_PREFIX == "devcake/"
    assert mission_branch("linear", "DEV-35") == "devcake/LINEAR-DEV-35"
    # empty instance = missing provenance — must fail loudly, never mint
    # an ambiguous devcake/-KEY branch
    import pytest as _pytest
    with _pytest.raises(ValueError, match="provenance"):
        mission_branch("", "DEV-35")


GH_PR_LIST_ITEM = {"number": 8, "html_url": "https://gh/pr/8", "state": "open",
                   "merged_at": None}
GH_PR_MERGED_ITEM = {"number": 9, "html_url": "https://gh/pr/9", "state": "closed",
                     "merged_at": "2026-07-13T00:00:00Z"}
GL_MR_OPEN = {"iid": 8, "web_url": "https://gl/mr/8", "state": "opened"}
GL_MR_MERGED = {"iid": 9, "web_url": "https://gl/mr/9", "state": "merged"}


def test_get_pr_by_branch_shape_parity():
    g = stub_req(gh(), [GH_PR_LIST_ITEM])
    pr = run_coro(g.get_pr_by_branch("devcake/DEV-1"))
    assert pr == PullRequest(number=8, url="https://gh/pr/8", state="open",
                             merged=False)
    g = stub_req(gh(), [GH_PR_MERGED_ITEM])
    pr = run_coro(g.get_pr_by_branch("devcake/DEV-1"))
    assert pr.merged is True and pr.state == "closed"   # merged from merged_at

    l = stub_req(gl(), [GL_MR_OPEN])
    mr = run_coro(l.get_pr_by_branch("devcake/DEV-1"))
    # GitLab "opened" normalizes to the same open shape
    assert mr == PullRequest(number=8, url="https://gl/mr/8", state="open",
                             merged=False)
    l = stub_req(gl(), [GL_MR_MERGED])
    mr = run_coro(l.get_pr_by_branch("devcake/DEV-1"))
    # "merged" state → closed + merged=True (identical to GitHub semantics)
    assert mr.state == "closed" and mr.merged is True

    assert run_coro(stub_req(gh(), []).get_pr_by_branch("x")) is None
    assert run_coro(stub_req(gl(), []).get_pr_by_branch("x")) is None


def test_pr_state_shape_parity():
    s = run_coro(stub_req(gh(), {"number": 8, "html_url": "u", "state": "closed",
                                 "merged": True}).pr_state(8))
    assert s == PullRequest(number=8, url="u", state="closed", merged=True)
    s = run_coro(stub_req(gl(), GL_MR_MERGED).pr_state(9))
    assert s == PullRequest(number=9, url="https://gl/mr/9", state="closed",
                            merged=True)


def test_branch_protection_dto():
    g = stub_req(gh(), {"protected": True})
    p = run_coro(g.default_branch_protection())
    assert isinstance(p, BranchProtection) and p.protected is True

    async def gl_404(method, path, **kw):
        raise ForgeError("nope", status=404)
    l = gl()
    l._req = gl_404
    p = run_coro(l.default_branch_protection())
    assert p == BranchProtection(protected=False, requires_reviews=None)


def test_health_probe_requires_repository_write_access():
    writable = run_coro(stub_req(
        gh(), {"permissions": {"pull": True, "push": True}}).health_probe())
    readonly = run_coro(stub_req(
        gh(), {"permissions": {"pull": True, "push": False}}).health_probe())
    assert writable == ForgeHealth(ok=True, repository="o/r",
                                   can_push=True, can_read=True)
    assert not readonly.ok and "lacks push" in readonly.detail

    gitlab = run_coro(stub_req(gl(), {
        "path_with_namespace": "o/r",
        "permissions": {"project_access": {"access_level": 30}},
    }).health_probe())
    assert gitlab.ok and gitlab.can_push and gitlab.repository == "o/r"


def test_health_probe_reports_read_access():
    """can_read distinguishes "readable but not writable" (the EXPECTED state
    of a reference-only repo — founder decision 2026-07-15) from "no access
    at all": ForgeRuntime treats the former as healthy for RO-only repos.
    Rule: the repository GET succeeded ⇒ can_read, whatever the push bit."""
    for forge, payload in (
        (gh(), {"permissions": {"pull": True, "push": False}}),
        (gl(), {"path_with_namespace": "o/r",
                "permissions": {"project_access": {"access_level": 20}}}),
        (GiteaForge("http://gitea:3300/o/r", "tok"),
         {"permissions": {"pull": True, "push": False}}),
    ):
        h = run_coro(stub_req(forge, payload).health_probe())
        assert h.can_read and not h.ok and not h.can_push, type(forge).__name__
    # a credential that cannot even see the repo reads as can_read=False
    assert not _probe_error(gh(), 401).can_read
    assert not _probe_error(gl(), 404).can_read


def _probe_error(forge, status, text="err"):
    async def _req(method, path, **kw):
        raise ForgeError(f"GET → {status}: {text}", status=status)
    forge._req = _req
    return run_coro(forge.health_probe())


def test_health_probe_distinguishes_transient_from_credential_failure():
    """Only definitive credential/permission failures may latch the global
    forge breaker; 5xx/network/rate-limit outcomes are marked transient."""
    assert _probe_error(gh(), 500).transient
    assert not _probe_error(gh(), 401).transient
    assert not _probe_error(gh(), 404).transient
    assert _probe_error(gh(), 403, "API rate limit exceeded for install").transient
    assert not _probe_error(gh(), 403, "Resource not accessible").transient
    assert _probe_error(gl(), 502).transient
    assert not _probe_error(gl(), 401).transient
    # the success shape stays non-transient by default
    ok = run_coro(stub_req(gh(), {"permissions": {"push": True}}).health_probe())
    assert ok.ok and not ok.transient


def test_api_base_defaults_and_overrides():
    assert gh().api == "https://api.github.com"
    assert GitHubForge("https://github.com/o/r", "t",
                       api_base="https://ghe.corp/api/v3").api == "https://ghe.corp/api/v3"
    assert gl().base == "https://gitlab.com"
    # self-hosted: derived from the repo URL's origin (no extra config needed)
    self_hosted = GitLabForge("https://gitlab.corp.example/grp/repo.git", "t")
    assert self_hosted.base == "https://gitlab.corp.example"
    assert self_hosted.project == "grp%2Frepo"
    # api_base override wins without corrupting the project path
    ov = GitLabForge("https://gitlab.corp.example/grp/repo", "t",
                     api_base="https://gitlab-api.corp.example")
    assert ov.base == "https://gitlab-api.corp.example"
    assert ov.project == "grp%2Frepo"


# ── ForgeDescriptor completeness + registry ──────────────────────────────────

from devcake.adapters.registry import forges, make_forge
from devcake.config import RepoInstance
from devcake.ports.forge import ForgeDescriptor


def test_registry_covers_all_forges_and_constructs():
    d = forges()
    assert set(d) == {"github", "gitlab", "gitea"}
    assert all(isinstance(v, ForgeDescriptor) for v in d.values())
    assert isinstance(make_forge(RepoInstance(forge="github",
                                              url="https://github.com/o/r")),
                      GitHubForge)
    gl_inst = RepoInstance(forge="gitlab", url="https://gitlab.corp/o/r",
                           api_base="https://gitlab-api.corp")
    f = make_forge(gl_inst)
    assert isinstance(f, GitLabForge) and f.base == "https://gitlab-api.corp"
    gt = make_forge(RepoInstance(forge="gitea",
                                 url="http://gitea:3000/devcake-internal/x-y"))
    assert isinstance(gt, GiteaForge) and gt.api == "http://gitea:3000"


@pytest.mark.parametrize("cls", [GitHubForge, GitLabForge, GiteaForge])
def test_descriptor_complete_and_renderable(cls):
    import re as _re
    d = cls.descriptor
    for field in ("id", "display_name", "pr_instructions", "clone_user",
                  "git_user_name", "git_email", "pr_noun"):
        assert getattr(d, field), f"{cls.__name__}.descriptor.{field} empty"
    for field in ("cli_token_envs", "secret_env_vars"):
        assert getattr(d, field), f"{cls.__name__}.descriptor.{field} empty"
    # token_patterns/secret_shape_prefixes MAY be deliberately empty (Gitea:
    # 40-hex tokens collide with git SHAs — value registration is the
    # redaction line, docs/14 §5); when present they must compile/behave
    # templates must render without KeyError against the documented placeholders
    d.pr_instructions.format(key="DEV-1", title="t", default="main",
                             branch="devcake/DEV-1")
    for pat in d.token_patterns:
        _re.compile(pat)


def test_unknown_forge_rejected_by_config():
    with pytest.raises(Exception, match="unknown forge"):
        RepoInstance(forge="fossil", url="https://x/y/z")


def test_gitlab_file_content_percent_encodes_ref():
    """Audit A16: GitHub/Gitea percent-encode path+ref (c57189f) but GitLab
    left `ref` raw — a '#'/'?'/space in a branch name corrupted the URL."""
    import asyncio
    ad = GitLabForge("https://gitlab.com/o/r", "tok")
    seen = {}

    async def _req(method, path, raw=False):
        seen["path"] = path
        return b"data"

    ad._req = _req
    asyncio.new_event_loop().run_until_complete(
        ad.file_content("a b.txt", "feature/x y#z"))
    assert "a%20b.txt" in seen["path"]
    assert "ref=feature%2Fx%20y%23z" in seen["path"]
