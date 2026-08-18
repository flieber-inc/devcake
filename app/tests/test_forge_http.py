"""Hermetic HTTP contract for ForgePort adapters (auth headers + URL assembly).

Domain tables in test_forge.py stub _req and never exercise vendor HTTP.
These tests inject httpx.MockTransport so empty _headers() or a broken
URL f-string fails the suite. Precedent: test_pmo_contract.py,
test_internal_forge_provision.py.
"""
from __future__ import annotations

import asyncio

import httpx

from devcake.adapters.github.adapter import GitHubForge
from devcake.adapters.gitea.adapter import GiteaForge
from devcake.adapters.gitlab.adapter import GitLabForge


def run_coro(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


def test_github_health_probe_sends_bearer_and_repo_url():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert str(request.url) == "https://api.github.com/repos/o/r"
        assert request.headers.get("Authorization") == "Bearer gh-write-tok"
        assert request.headers.get("Accept") == "application/vnd.github+json"
        return httpx.Response(200, json={"permissions": {"push": True}})

    forge = GitHubForge(
        "https://github.com/o/r", "gh-write-tok",
        transport=httpx.MockTransport(handler),
    )
    health = run_coro(forge.health_probe())
    assert health.ok is True
    assert health.can_push is True
    assert len(seen) == 1


def test_github_reviewer_token_on_approve():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST"
        assert str(request.url).endswith("/pulls/8/reviews")
        assert request.headers.get("Authorization") == "Bearer gh-reviewer-tok"
        return httpx.Response(200, json={"id": 1})

    forge = GitHubForge(
        "https://github.com/o/r", "gh-write-tok", "gh-reviewer-tok",
        transport=httpx.MockTransport(handler),
    )
    assert run_coro(forge.approve(8)) is True
    assert len(seen) == 1


def test_github_enterprise_api_base_in_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://ghe.example/api/v3/repos/o/r")
        assert request.headers.get("Authorization") == "Bearer tok"
        return httpx.Response(200, json={"permissions": {"push": True}})

    forge = GitHubForge(
        "https://github.com/o/r", "tok",
        api_base="https://ghe.example/api/v3",
        transport=httpx.MockTransport(handler),
    )
    assert run_coro(forge.health_probe()).ok is True


def test_gitlab_health_probe_sends_private_token_and_project_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://gitlab.com/api/v4/projects/group%2Fproj")
        assert request.headers.get("PRIVATE-TOKEN") == "gl-write-tok"
        return httpx.Response(200, json={
            "permissions": {
                "project_access": {"access_level": 40},
                "group_access": None,
            },
        })

    forge = GitLabForge(
        "https://gitlab.com/group/proj", "gl-write-tok",
        transport=httpx.MockTransport(handler),
    )
    health = run_coro(forge.health_probe())
    assert health.ok is True


def test_gitlab_reviewer_token_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("PRIVATE-TOKEN") == "gl-reviewer"
        return httpx.Response(200, json={"id": 1})

    forge = GitLabForge(
        "https://gitlab.com/group/proj", "gl-write", "gl-reviewer",
        transport=httpx.MockTransport(handler),
    )
    assert run_coro(forge.approve(3)) is True


def test_gitea_health_probe_sends_token_auth_and_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://git.example/api/v1/repos/o/r"
        assert request.headers.get("Authorization") == "token gitea-tok"
        return httpx.Response(200, json={"permissions": {"push": True}})

    forge = GiteaForge(
        "https://git.example/o/r", "gitea-tok",
        transport=httpx.MockTransport(handler),
    )
    health = run_coro(forge.health_probe())
    assert health.ok is True
    assert health.can_push is True


def test_gitea_reviewer_token_header():
    """Gitea review event is APPROVED (not GitHub's APPROVE) — live-verified
    M11; a mutant posting APPROVE would 422 and still look green if we only
    checked the Authorization header."""
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "token gitea-reviewer"
        assert request.method == "POST"
        assert str(request.url).endswith("/pulls/2/reviews")
        body = _json.loads(request.content.decode())
        assert body["event"] == "APPROVED"
        return httpx.Response(200, json={"id": 1})

    forge = GiteaForge(
        "https://git.example/o/r", "gitea-tok", "gitea-reviewer",
        transport=httpx.MockTransport(handler),
    )
    assert run_coro(forge.approve(2)) is True
    assert run_coro(GiteaForge(
        "https://git.example/o/r", "gitea-tok", None,
        transport=httpx.MockTransport(handler),
    ).approve(2)) is False


def test_github_request_asserts_auth_header_on_wire():
    """Mutant bar: assert the Authorization header on the request itself
    (not only health.ok). Empty _headers() fails here without relying on
    health_probe mapping 401 → ok=False."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization", ""))
        return httpx.Response(200, json={"permissions": {"push": True}})

    forge = GitHubForge(
        "https://github.com/o/r", "must-be-sent",
        transport=httpx.MockTransport(handler),
    )
    assert run_coro(forge.health_probe()).ok is True
    assert seen == ["Bearer must-be-sent"]


def test_github_post_pr_comment_is_write_path_with_auth():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url).endswith("/issues/3/comments")
        assert request.headers.get("Authorization") == "Bearer gh-write"
        return httpx.Response(201, json={"id": 1})

    forge = GitHubForge(
        "https://github.com/o/r", "gh-write",
        transport=httpx.MockTransport(handler),
    )
    run_coro(forge.post_pr_comment(3, "hello"))


def test_github_post_pr_comment_redacts_secret_shapes():
    """docs/06 §1: forge-bound PR comment bodies pass through security.redact."""
    from devcake.security import MASK

    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read().decode())
        return httpx.Response(201, json={"id": 1})

    secret = "ghp_" + ("a" * 36)
    forge = GitHubForge(
        "https://github.com/o/r", "gh-write",
        transport=httpx.MockTransport(handler),
    )
    run_coro(forge.post_pr_comment(3, f"see {secret} in the report"))
    assert len(bodies) == 1
    assert secret not in bodies[0]
    assert MASK in bodies[0]


def test_gitlab_post_pr_comment_redacts_secret_shapes():
    """docs/06 §1: same redact chokepoint on GitLab MR notes."""
    from devcake.security import MASK

    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read().decode())
        return httpx.Response(201, json={"id": 1})

    secret = "glpat-" + ("b" * 20)
    forge = GitLabForge(
        "https://gitlab.com/group/proj", "gl-write",
        transport=httpx.MockTransport(handler),
    )
    run_coro(forge.post_pr_comment(3, f"token {secret} leaked"))
    assert len(bodies) == 1
    assert secret not in bodies[0]
    assert MASK in bodies[0]


def test_github_approve_same_token_is_noop_when_self_approval_blocked():
    """self_approval_blocked=True: write token pasted as reviewer is not a
    distinct reviewer — return False without a wire call (docs/06 §4)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": 1})

    same = "gh-same-tok"
    forge = GitHubForge(
        "https://github.com/o/r", same, same,
        transport=httpx.MockTransport(handler),
    )
    assert run_coro(forge.approve(8)) is False
    assert seen == []


def test_gitlab_approve_same_token_allowed_when_self_approval_not_blocked():
    """self_approval_blocked=False: GitLab allows author approve by default —
    same write/reviewer token still posts the approve call."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers.get("PRIVATE-TOKEN") == "gl-same"
        return httpx.Response(201, json={"id": 1})

    same = "gl-same"
    forge = GitLabForge(
        "https://gitlab.com/group/proj", same, same,
        transport=httpx.MockTransport(handler),
    )
    assert run_coro(forge.approve(3)) is True
    assert len(seen) == 1


def test_approve_returns_false_without_reviewer_token():
    """Port contract: approve() is False when no reviewer token is configured."""
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"approve must not hit the wire: {request.url}")

    gh = GitHubForge(
        "https://github.com/o/r", "gh-write",
        transport=httpx.MockTransport(boom),
    )
    gl = GitLabForge(
        "https://gitlab.com/group/proj", "gl-write",
        transport=httpx.MockTransport(boom),
    )
    assert run_coro(gh.approve(1)) is False
    assert run_coro(gl.approve(1)) is False


def test_github_empty_token_does_not_send_illegal_bearer():
    """ADR-0011 class: empty token must not build Authorization: Bearer <empty>.
    Fail closed before send; health_probe maps the error to ok=False."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    forge = GitHubForge(
        "https://github.com/o/r", "",
        transport=httpx.MockTransport(handler),
    )
    health = run_coro(forge.health_probe())
    assert seen == [], "must not hit the wire with an empty Bearer token"
    assert health.ok is False
