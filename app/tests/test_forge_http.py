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
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "token gitea-reviewer"
        return httpx.Response(200, json={"id": 1})

    forge = GiteaForge(
        "https://git.example/o/r", "gitea-tok", "gitea-reviewer",
        transport=httpx.MockTransport(handler),
    )
    assert run_coro(forge.approve(2)) is True


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
