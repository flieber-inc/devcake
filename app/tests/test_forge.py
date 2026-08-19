"""docs/06 §5 port contract, both v0 adapters: mergeable() tri-state signal
mapping, the transient-409 merge retry, and the normalized ForgeError type
(GitLab must never leak httpx exceptions). No network — _req is stubbed."""
import asyncio

import httpx
import pytest

from devcake.adapters.github.adapter import GitHubForge
from devcake.ports.forge import BranchProtection, ForgeError
from devcake.adapters.gitea.adapter import GiteaForge
from devcake.adapters.gitlab.adapter import GitLabForge


def gh():
    return GitHubForge("https://github.com/o/r", "tok")


def gl():
    return GitLabForge("https://gitlab.com/o/r", "tok")


def gt():
    return GiteaForge("http://gitea:3000/o/r", "tok")


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


@pytest.mark.parametrize("payload,expected", [
    ({"mergeable": True}, True),
    ({"mergeable": False}, False),
    ({}, None),                          # absent → unknown (boolean-only forge)
    ({"mergeable": None}, None),
])
def test_gitea_mergeable_boolean_only(payload, expected):
    """docs/06 §7a: Gitea has no mergeable_state — True/False/absent →
    True/False/None (capability mergeable_tristate=False)."""
    assert run_coro(stub_req(gt(), payload).mergeable(8)) is expected


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
            # not merged — redelivery probe then re-raise merge error
            return {"number": 8, "html_url": "https://x/8", "state": "open",
                    "merged": False, "web_url": "https://x/8", "iid": 8}
        raise ForgeError("conflicts", status=405)
    forge2._req = conflict_405
    with pytest.raises(ForgeError) as e:
        run_coro(forge2.merge(8))
    # one merge attempt + one already-merged probe; no 409-style retries
    assert len(calls2) == 2 and e.value.status == 405


def test_gitea_merge_retries_try_again_later_405(monkeypatch):
    """docs/06 §7a: only the transient "Please try again later" 405 retries."""
    async def no_sleep(_):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    forge, calls = gt(), []

    async def _req(method, path, **kw):
        calls.append((method, path))
        if len(calls) < 3:
            raise ForgeError("Please try again later", status=405)
    forge._req = _req
    run_coro(forge.merge(8))
    assert len(calls) == 3
    assert all(m == "POST" and path.endswith("/merge") for m, path in calls)


def test_gitea_merge_definitive_405_probes_already_merged(monkeypatch):
    """Approvals/conflict 405s must not retry as transient; redelivery probe
    still absorbs a successful-but-redelivered merge."""
    async def no_sleep(_):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    forge, calls = gt(), []

    async def approvals_405(method, path, **kw):
        calls.append((method, path))
        if method == "GET":
            return {"number": 8, "html_url": "https://g/8", "state": "open",
                    "merged": False}
        raise ForgeError("Does not have enough approvals", status=405)
    forge._req = approvals_405
    with pytest.raises(ForgeError) as e:
        run_coro(forge.merge(8))
    assert e.value.status == 405
    assert len(calls) == 2                     # one POST + one GET probe
    assert calls[0][0] == "POST" and calls[1][0] == "GET"

    forge2, calls2 = gt(), []

    async def already_merged(method, path, **kw):
        calls2.append((method, path))
        if method == "GET":
            return {"number": 8, "html_url": "https://g/8", "state": "closed",
                    "merged": True}
        raise ForgeError("already merged", status=405)
    forge2._req = already_merged
    run_coro(forge2.merge(8))                  # probe absorbs the failure
    assert len(calls2) == 2


def test_github_merge_already_merged_is_success():
    """Redelivery after a successful merge must not report failure."""
    forge, calls = gh(), []

    async def merged_405(method, path, **kw):
        calls.append((method, path))
        if method == "GET":
            return {"number": 8, "html_url": "https://x/8", "state": "closed",
                    "merged": True}
        raise ForgeError("already merged", status=405)

    forge._req = merged_405
    run_coro(forge.merge(8))  # must not raise
    assert calls[0][0] == "PUT" and calls[1][0] == "GET"


def test_gitlab_merge_already_merged_is_success():
    """GitLab derives merged from MR state == 'merged'."""
    forge, calls = gl(), []

    async def merged_405(method, path, **kw):
        calls.append((method, path))
        if method == "GET":
            return {"iid": 8, "web_url": "https://x/8", "state": "merged"}
        raise ForgeError("already merged", status=405)

    forge._req = merged_405
    run_coro(forge.merge(8))  # must not raise
    assert calls[0][0] == "PUT" and calls[1][0] == "GET"


def test_github_branch_protection_detail_403_keeps_protected_flag():
    """branch_protection_read=admin: classic protection may 403 without admin
    scope — still return the branch `protected` flag; requires_reviews stays
    None rather than inventing a false negative."""
    g = gh()
    calls = []

    async def _req(method, path, **kw):
        calls.append(path)
        if path.startswith("/branches/") and "/protection" not in path and not path.startswith("/rules"):
            return {"protected": True, "name": "main"}
        if "/protection" in path or path.startswith("/rules"):
            raise ForgeError("need admin", status=403)
        raise AssertionError(path)

    g._req = _req
    p = run_coro(g.default_branch_protection())
    assert p == BranchProtection(protected=True, requires_reviews=None)
    assert any("/protection" in c for c in calls)


def test_gitlab_branch_protection_non_404_is_unreadable():
    """Maintainer-readable protection: 403 → None (unknown), not unprotected."""
    l = gl()

    async def gl_403(method, path, **kw):
        raise ForgeError("forbidden", status=403)

    l._req = gl_403
    assert run_coro(l.default_branch_protection()) is None


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

    # Gitea: server ignores `head` — adapter filters client-side on head.ref
    gitea_open = {"number": 8, "html_url": "https://gt/pr/8", "state": "open",
                  "merged": False, "head": {"ref": "devcake/DEV-1"}}
    gitea_other = {"number": 7, "html_url": "https://gt/pr/7", "state": "open",
                   "merged": False, "head": {"ref": "someone-else"}}
    pr = run_coro(stub_req(gt(), [gitea_other, gitea_open]).get_pr_by_branch(
        "devcake/DEV-1"))
    assert pr == PullRequest(number=8, url="https://gt/pr/8", state="open",
                             merged=False)
    assert run_coro(stub_req(gt(), [gitea_other]).get_pr_by_branch(
        "devcake/DEV-1")) is None


def test_gitea_get_pr_by_branch_no_server_head_filter():
    """pr_list_head_filter=False: requests must not rely on a `head=` query;
    matching is client-side across the listed page(s)."""
    seen: list[str] = []
    forge = gt()

    async def _req(method, path, **kw):
        seen.append(path)
        return [
            {"number": 1, "html_url": "https://gt/1", "state": "open",
             "merged": False, "head": {"ref": "noise"}},
            {"number": 2, "html_url": "https://gt/2", "state": "open",
             "merged": False, "head": {"ref": "devcake/DEV-9"}},
        ]

    forge._req = _req
    pr = run_coro(forge.get_pr_by_branch("devcake/DEV-9"))
    assert pr is not None and pr.number == 2
    assert len(seen) == 1
    assert "head=" not in seen[0]
    assert "state=all" in seen[0]


def test_gitea_get_pr_by_branch_finds_match_beyond_page_one():
    """Busy repos (>50 PRs) hide the mission branch past page 1 — the
    adapter must walk pages and still return the newest matching PR."""
    target = "devcake/DEV-page2"
    page1 = [
        {"number": i, "html_url": f"https://gt/{i}", "state": "open",
         "merged": False, "head": {"ref": f"noise-{i}"}}
        for i in range(1, 51)
    ]
    page2 = [
        {"number": 51, "html_url": "https://gt/51", "state": "closed",
         "merged": True, "head": {"ref": target}},
        {"number": 52, "html_url": "https://gt/52", "state": "open",
         "merged": False, "head": {"ref": "other"}},
    ]
    pages = {1: page1, 2: page2}
    forge = gt()

    async def _req(method, path, **kw):
        assert "page=" in path
        page = int(path.split("page=")[-1].split("&")[0])
        return pages.get(page, [])

    forge._req = _req
    pr = run_coro(forge.get_pr_by_branch(target))
    assert pr is not None
    assert pr.number == 51
    assert pr.merged is True
    assert pr.state == "closed"


def test_gitea_pr_files_concatenates_across_pages():
    """Large changesets span pages — pr_files must return every path."""
    from devcake.ports.forge import PRFile

    page1 = [
        {"filename": f"f{i}.py", "status": "modified",
         "additions": 1, "deletions": 0}
        for i in range(50)
    ]
    page2 = [
        {"filename": "tail.py", "status": "added",
         "additions": 3, "deletions": 1},
    ]
    pages = {1: page1, 2: page2}
    forge = gt()

    async def _req(method, path, **kw):
        assert "/pulls/9/files" in path
        page = int(path.split("page=")[-1].split("&")[0])
        return pages.get(page, [])

    forge._req = _req
    result = run_coro(forge.pr_files(9))
    files = result.files
    assert result.truncated is False
    assert len(files) == 51
    assert files[0] == PRFile(path="f0.py", status="modified",
                              additions=1, deletions=0)
    assert files[-1] == PRFile(path="tail.py", status="added",
                               additions=3, deletions=1)


def test_get_pr_by_branch_encodes_hash_in_query():
    """Forge-issue mission keys mint branches with `#` (owner/repo#N). An
    unescaped `#` is a URL fragment — the forge never sees the head /
    source_branch filter, so merge_sweep cannot complete after a merge."""
    from urllib.parse import quote

    branch = "devcake/DEVCAKEMEM-example-org/devcake-memories#3"

    seen: list[str] = []
    g = gh()

    async def gh_req(method, path, **kw):
        seen.append(path)
        return [GH_PR_MERGED_ITEM]

    g._req = gh_req
    pr = run_coro(g.get_pr_by_branch(branch))
    assert pr is not None and pr.number == 9
    assert len(seen) == 1
    assert f"head={quote(f'o:{branch}', safe='')}" in seen[0]
    assert "#" not in seen[0]

    seen.clear()
    l = gl()

    async def gl_req(method, path, **kw):
        seen.append(path)
        return [GL_MR_MERGED]

    l._req = gl_req
    mr = run_coro(l.get_pr_by_branch(branch))
    assert mr is not None and mr.number == 9
    assert len(seen) == 1
    assert f"source_branch={quote(branch, safe='')}" in seen[0]
    assert "#" not in seen[0]


def test_pr_state_shape_parity():
    s = run_coro(stub_req(gh(), {"number": 8, "html_url": "u", "state": "closed",
                                 "merged": True}).pr_state(8))
    assert s == PullRequest(number=8, url="u", state="closed", merged=True)
    s = run_coro(stub_req(gl(), GL_MR_MERGED).pr_state(9))
    assert s == PullRequest(number=9, url="https://gl/mr/9", state="closed",
                            merged=True)
    s = run_coro(stub_req(gt(), {"number": 8, "html_url": "https://gt/8",
                                 "state": "closed", "merged": True}).pr_state(8))
    assert s == PullRequest(number=8, url="https://gt/8", state="closed",
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


def test_gitea_token_patterns_deliberately_empty():
    """40-hex Gitea tokens collide with git SHAs — value registration is the
    only redaction line (ADR-0010). Distinct from gitea_issues PMO patterns."""
    assert GiteaForge.descriptor.token_patterns == []


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
    # redaction line, docs/14 §7); when present they must compile/behave
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


def test_gitlab_pr_files_sets_truncated_when_overflow():
    """GitLab /changes overflow=true withholds paths — pr_files must surface
    truncated=True so delivery never silently over-claims completeness."""
    from devcake.ports.forge import PRFilesResult

    forge = gl()
    calls: list[str] = []

    async def _req(method, path, **kw):
        calls.append(path)
        return {
            "overflow": True,
            "changes": [
                {"new_path": "kept.py", "new_file": True},
                {"new_path": "also.py", "deleted_file": False,
                 "new_file": False, "renamed_file": False},
            ],
        }

    forge._req = _req
    result = run_coro(forge.pr_files(42))
    assert isinstance(result, PRFilesResult)
    assert result.truncated is True
    assert [f.path for f in result.files] == ["kept.py", "also.py"]
    # overflow remains after the access_raw_diffs retry
    assert any("access_raw_diffs=true" in p for p in calls)


def test_gitlab_pr_files_clears_truncated_via_access_raw_diffs():
    """When /changes reports overflow, retry once with access_raw_diffs=true;
    a clear response means truncated=False and the fuller path set."""
    from devcake.ports.forge import PRFilesResult

    forge = gl()
    calls: list[str] = []

    async def _req(method, path, **kw):
        calls.append(path)
        if "access_raw_diffs=true" in path:
            return {
                "overflow": False,
                "changes": [
                    {"new_path": "a.py", "new_file": True},
                    {"new_path": "b.py", "new_file": True},
                    {"new_path": "c.py", "new_file": True},
                ],
            }
        return {
            "overflow": True,
            "changes": [{"new_path": "a.py", "new_file": True}],
        }

    forge._req = _req
    result = run_coro(forge.pr_files(7))
    assert isinstance(result, PRFilesResult)
    assert result.truncated is False
    assert [f.path for f in result.files] == ["a.py", "b.py", "c.py"]
    assert calls[0].endswith("/merge_requests/7/changes")
    assert "access_raw_diffs=true" in calls[1]
