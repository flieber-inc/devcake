"""CAKE-181 — apply_default_branch_protection: shape derivation, no-weaken,
403 path, and hermetic Gitea round-trip (docs/06)."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from devcake.ports.forge import (
    ApplyProtectionResult,
    ForgeError,
    ProtectionShape,
    derive_protection_shape,
    distinct_reviewer_configured,
    is_as_strict_as,
    merge_strictest,
)


def run_coro(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


# ── shape derivation (public helpers on the port module) ─────────────────────

def test_derive_shape_uses_discovered_checks_never_hardcoded():
    shape = derive_protection_shape(
        discovered_status_checks=["ci/tests", "lint"],
        has_distinct_reviewer=False,
    )
    assert shape.require_pull_request is True
    assert shape.allow_force_push is False
    assert shape.allow_deletions is False
    assert shape.required_status_checks == ["ci/tests", "lint"]
    assert shape.required_approving_review_count == 0


def test_derive_shape_empty_checks_when_repo_has_no_ci():
    shape = derive_protection_shape(
        discovered_status_checks=[],
        has_distinct_reviewer=False,
    )
    assert shape.required_status_checks == []


def test_derive_shape_requires_one_approval_only_with_distinct_reviewer():
    without = derive_protection_shape(
        discovered_status_checks=[], has_distinct_reviewer=False)
    with_rev = derive_protection_shape(
        discovered_status_checks=[], has_distinct_reviewer=True)
    assert without.required_approving_review_count == 0
    assert with_rev.required_approving_review_count == 1


def test_distinct_reviewer_configured_requires_different_nonempty_token():
    assert distinct_reviewer_configured("write", "reviewer") is True
    assert distinct_reviewer_configured("write", "write") is False
    assert distinct_reviewer_configured("write", None) is False
    assert distinct_reviewer_configured("write", "") is False
    assert distinct_reviewer_configured("write", "  ") is False


# ── no-weaken ───────────────────────────────────────────────────────────────

def test_already_as_strict_when_current_meets_or_exceeds_desired():
    desired = ProtectionShape(
        require_pull_request=True,
        allow_force_push=False,
        allow_deletions=False,
        required_status_checks=["ci"],
        required_approving_review_count=1,
    )
    current = ProtectionShape(
        require_pull_request=True,
        allow_force_push=False,
        allow_deletions=False,
        required_status_checks=["ci", "lint"],
        required_approving_review_count=2,
    )
    assert is_as_strict_as(current, desired) is True


def test_not_as_strict_when_missing_required_check_or_approval():
    desired = ProtectionShape(
        required_status_checks=["ci"],
        required_approving_review_count=1,
    )
    missing_check = ProtectionShape(
        required_status_checks=[],
        required_approving_review_count=1,
    )
    missing_approval = ProtectionShape(
        required_status_checks=["ci"],
        required_approving_review_count=0,
    )
    assert is_as_strict_as(missing_check, desired) is False
    assert is_as_strict_as(missing_approval, desired) is False
    assert is_as_strict_as(None, desired) is False


def test_merge_strictest_unions_checks_and_takes_max_approvals():
    current = ProtectionShape(
        required_status_checks=["lint"],
        required_approving_review_count=2,
        allow_force_push=False,
        allow_deletions=True,
    )
    desired = ProtectionShape(
        required_status_checks=["ci"],
        required_approving_review_count=1,
        allow_force_push=False,
        allow_deletions=False,
    )
    merged = merge_strictest(current, desired)
    assert merged.required_status_checks == ["ci", "lint"]
    assert merged.required_approving_review_count == 2
    assert merged.allow_deletions is False
    assert merged.require_pull_request is True


# ── adapter apply (MockTransport) ────────────────────────────────────────────

def test_gitea_apply_discovers_checks_and_round_trips_protected():
    """Apply writes discovered contexts; subsequent read shows protected=True."""
    from devcake.adapters.gitea.adapter import GiteaForge

    state: dict = {"rule": None, "posted": None}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = request.url.path
        if request.method == "GET" and path.endswith("/branches/main"):
            return httpx.Response(200, json={
                "name": "main",
                "commit": {"id": "abc123"},
            })
        if request.method == "GET" and "/statuses/abc123" in path:
            return httpx.Response(200, json=[
                {"context": "ci/tests", "status": "success"},
                {"context": "lint", "status": "success"},
            ])
        if request.method == "GET" and path.endswith("/branch_protections"):
            if state["rule"] is None:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[state["rule"]])
        if request.method == "GET" and path.endswith("/branch_protections/main"):
            if state["rule"] is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=state["rule"])
        if request.method == "POST" and path.endswith("/branch_protections"):
            body = json.loads(request.content.decode())
            state["posted"] = body
            state["rule"] = {**body, "branch_name": body.get("branch_name", "main")}
            return httpx.Response(201, json=state["rule"])
        return httpx.Response(500, json={"message": f"unhandled {request.method} {url}"})

    forge = GiteaForge(
        "https://git.example/o/r", "write-tok", "reviewer-tok",
        transport=httpx.MockTransport(handler),
    )
    result = run_coro(forge.apply_default_branch_protection("main"))
    assert isinstance(result, ApplyProtectionResult)
    assert result.outcome == "applied"
    assert result.shape.required_status_checks == ["ci/tests", "lint"]
    assert result.shape.required_approving_review_count == 1
    assert state["posted"]["enable_status_check"] is True
    assert state["posted"]["status_check_contexts"] == ["ci/tests", "lint"]
    assert state["posted"]["required_approvals"] == 1
    assert state["posted"].get("enable_push") is False

    prot = run_coro(forge.default_branch_protection("main"))
    assert prot is not None and prot.protected is True
    assert prot.requires_reviews is True


def test_gitea_apply_already_as_strict_is_noop():
    from devcake.adapters.gitea.adapter import GiteaForge

    existing = {
        "branch_name": "main",
        "enable_push": False,
        "enable_force_push": False,
        "enable_status_check": True,
        "status_check_contexts": ["ci/tests"],
        "required_approvals": 1,
    }
    writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            writes.append(f"{request.method} {path}")
        if request.method == "GET" and path.endswith("/branches/main"):
            return httpx.Response(200, json={"commit": {"id": "abc"}})
        if request.method == "GET" and "/statuses/" in path:
            return httpx.Response(200, json=[{"context": "ci/tests"}])
        if request.method == "GET" and path.endswith("/branch_protections"):
            return httpx.Response(200, json=[existing])
        if request.method == "GET" and path.endswith("/branch_protections/main"):
            return httpx.Response(200, json=existing)
        return httpx.Response(500, text="unexpected")

    forge = GiteaForge(
        "https://git.example/o/r", "write-tok", "reviewer-tok",
        transport=httpx.MockTransport(handler),
    )
    result = run_coro(forge.apply_default_branch_protection("main"))
    assert result.outcome == "already_as_strict"
    assert writes == []


def test_gitea_apply_403_names_token_and_permission():
    from devcake.adapters.gitea.adapter import GiteaForge

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/branches/main"):
            return httpx.Response(200, json={"commit": {"id": "abc"}})
        if request.method == "GET" and "/statuses/" in path:
            return httpx.Response(200, json=[])
        if request.method == "GET" and path.endswith("/branch_protections"):
            return httpx.Response(200, json=[])
        if request.method == "GET" and path.endswith("/branch_protections/main"):
            return httpx.Response(404, json={"message": "not found"})
        if request.method == "POST" and path.endswith("/branch_protections"):
            return httpx.Response(403, json={"message": "forbidden"})
        return httpx.Response(500, text="unexpected")

    forge = GiteaForge(
        "https://git.example/o/r", "write-tok",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ForgeError) as ei:
        run_coro(forge.apply_default_branch_protection("main"))
    msg = str(ei.value).lower()
    assert ei.value.status == 403
    assert "write" in msg and ("token" in msg or "credential" in msg)
    assert any(p in msg for p in ("admin", "protect", "permission", "scope"))


def test_github_apply_403_names_administration_permission():
    from devcake.adapters.github.adapter import GitHubForge

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/branches/main"):
            return httpx.Response(200, json={
                "protected": False,
                "commit": {"sha": "deadbeef"},
            })
        if request.method == "GET" and path.endswith("/status"):
            return httpx.Response(200, json={"statuses": []})
        if request.method == "GET" and path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        if request.method == "GET" and path.endswith("/protection"):
            return httpx.Response(404, json={"message": "Not Found"})
        if request.method == "PUT" and path.endswith("/protection"):
            return httpx.Response(403, json={"message": "Must have admin rights"})
        return httpx.Response(500, text=f"unexpected {request.method} {path}")

    forge = GitHubForge(
        "https://github.com/o/r", "gh-write",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ForgeError) as ei:
        run_coro(forge.apply_default_branch_protection("main"))
    msg = str(ei.value).lower()
    assert ei.value.status == 403
    assert "write" in msg or "token" in msg
    assert "admin" in msg


def test_gitea_apply_no_reviewer_skips_required_approvals():
    from devcake.adapters.gitea.adapter import GiteaForge

    posted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/branches/main"):
            return httpx.Response(200, json={"commit": {"id": "abc"}})
        if request.method == "GET" and "/statuses/" in path:
            return httpx.Response(200, json=[])
        if request.method == "GET" and path.endswith("/branch_protections"):
            return httpx.Response(200, json=[])
        if request.method == "GET" and path.endswith("/branch_protections/main"):
            return httpx.Response(404, json={"message": "not found"})
        if request.method == "POST" and path.endswith("/branch_protections"):
            posted.update(json.loads(request.content.decode()))
            return httpx.Response(201, json={**posted, "branch_name": "main"})
        return httpx.Response(500, text="unexpected")

    forge = GiteaForge(
        "https://git.example/o/r", "write-tok",  # no reviewer
        transport=httpx.MockTransport(handler),
    )
    result = run_coro(forge.apply_default_branch_protection("main"))
    assert result.outcome == "applied"
    assert result.shape.required_approving_review_count == 0
    assert posted.get("required_approvals", 0) == 0


def test_github_apply_preserves_code_owners_and_restrictions():
    """PUT must merge DevCake constraints into the existing classic rule —
    never wipe require_code_owner_reviews or push restrictions (review #1)."""
    from devcake.adapters.github.adapter import GitHubForge

    existing = {
        "url": "https://api.github.com/repos/o/r/branches/main/protection",
        "required_status_checks": {
            "strict": True,
            "contexts": ["lint"],
            "checks": [{"context": "lint", "app_id": -1}],
        },
        "enforce_admins": {"enabled": True},
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": True,
            "required_approving_review_count": 1,
            "require_last_push_approval": False,
        },
        "restrictions": {
            "users": [{"login": "release-bot"}],
            "teams": [{"slug": "releasers"}],
            "apps": [],
        },
        "required_linear_history": {"enabled": True},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
        "required_conversation_resolution": {"enabled": True},
        "lock_branch": {"enabled": False},
        "allow_fork_syncing": {"enabled": False},
    }
    put_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/branches/main"):
            return httpx.Response(200, json={
                "protected": True,
                "commit": {"sha": "deadbeef"},
            })
        if request.method == "GET" and path.endswith("/status"):
            return httpx.Response(200, json={
                "statuses": [{"context": "ci", "state": "success"}],
            })
        if request.method == "GET" and "check-runs" in path:
            return httpx.Response(200, json={"check_runs": []})
        if request.method == "GET" and path.endswith("/protection"):
            return httpx.Response(200, json=existing)
        if request.method == "PUT" and path.endswith("/protection"):
            body = json.loads(request.content.decode())
            put_bodies.append(body)
            return httpx.Response(200, json=existing)
        return httpx.Response(500, text=f"unexpected {request.method} {path}")

    forge = GitHubForge(
        "https://github.com/o/r", "gh-write", "gh-reviewer",
        transport=httpx.MockTransport(handler),
    )
    result = run_coro(forge.apply_default_branch_protection("main"))
    assert result.outcome == "applied"
    assert put_bodies, "expected a protection PUT"
    body = put_bodies[0]
    reviews = body["required_pull_request_reviews"]
    assert reviews["require_code_owner_reviews"] is True
    assert reviews["dismiss_stale_reviews"] is True
    assert reviews["required_approving_review_count"] >= 1
    assert body["restrictions"] is not None
    # PUT form uses login / slug lists (GET returns objects).
    assert body["restrictions"]["users"] == ["release-bot"]
    assert body["restrictions"]["teams"] == ["releasers"]
    assert body.get("required_linear_history") is True
    assert body.get("required_conversation_resolution") is True
    contexts = (body.get("required_status_checks") or {}).get("contexts") or []
    assert "ci" in contexts and "lint" in contexts
    assert body["allow_force_pushes"] is False
    assert body["allow_deletions"] is False


def test_gitlab_apply_restores_protection_when_recreate_fails():
    """DELETE-then-POST must not leave the branch unprotected if POST fails
    (review #2) — capture prior rule and restore before raising."""
    from urllib.parse import unquote

    from devcake.adapters.gitlab.adapter import GitLabForge

    prior = {
        "name": "main",
        "push_access_levels": [{"access_level": 0}],
        "merge_access_levels": [{"access_level": 40}],
        "allow_force_push": False,
    }
    state = {"deleted": False, "restored": None, "posts": 0}
    proj = {
        "approvals_before_merge": 0,
        "only_allow_merge_if_pipeline_succeeds": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        method = request.method
        if method == "GET" and path.endswith("/repository/branches/main"):
            return httpx.Response(200, json={"commit": {"id": "abc"}})
        if method == "GET" and "/repository/commits/" in path and path.endswith(
                "/statuses"):
            return httpx.Response(200, json=[{"name": "ci"}])
        if method == "GET" and path.endswith("/protected_branches/main"):
            if state["deleted"] and state["restored"] is None:
                return httpx.Response(404, json={"message": "404"})
            return httpx.Response(200, json=state["restored"] or prior)
        if method == "GET" and path.rstrip("/").endswith("/projects/g/r"):
            return httpx.Response(200, json=proj)
        if method == "DELETE" and path.endswith("/protected_branches/main"):
            state["deleted"] = True
            return httpx.Response(204)
        if method == "POST" and path.endswith("/protected_branches"):
            state["posts"] += 1
            if state["posts"] == 1:
                # first recreate fails after DELETE
                return httpx.Response(500, json={"message": "boom"})
            # restore POST
            body = json.loads(request.content.decode())
            state["restored"] = {
                **prior, **body, "name": body.get("name", "main")}
            state["deleted"] = False
            return httpx.Response(201, json=state["restored"])
        if method == "PUT" and path.rstrip("/").endswith("/projects/g/r"):
            return httpx.Response(200, json=proj)
        return httpx.Response(500, text=f"unexpected {method} {path}")

    forge = GitLabForge(
        "https://gitlab.example/g/r", "gl-write", "gl-reviewer",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ForgeError):
        run_coro(forge.apply_default_branch_protection("main"))
    assert state["restored"] is not None, "expected restore of prior protection"
    assert state["deleted"] is False
    assert state["restored"].get("name") == "main"
    assert state["restored"].get("allow_force_push") is False


def test_gitea_apply_empty_enable_status_check_does_not_invent_star():
    """enable_status_check with empty contexts must not invent '*' (review #3);
    write must emit exactly the discovered contexts."""
    from devcake.adapters.gitea.adapter import GiteaForge

    existing = {
        "branch_name": "main",
        "enable_push": False,
        "enable_force_push": False,
        "enable_status_check": True,
        "status_check_contexts": [],
        "required_approvals": 0,
    }
    writes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/branches/main"):
            return httpx.Response(200, json={"commit": {"id": "abc"}})
        if request.method == "GET" and "/statuses/" in path:
            return httpx.Response(200, json=[{"context": "ci"}])
        if request.method == "GET" and path.endswith("/branch_protections"):
            return httpx.Response(200, json=[existing])
        if request.method == "GET" and path.endswith("/branch_protections/main"):
            return httpx.Response(200, json=existing)
        if request.method in ("POST", "PATCH") and "branch_protections" in path:
            body = json.loads(request.content.decode())
            writes.append(body)
            # keep rule updated for subsequent reads
            existing.update({k: v for k, v in body.items()})
            existing["branch_name"] = "main"
            return httpx.Response(200, json=existing)
        return httpx.Response(500, text=f"unexpected {request.method} {path}")

    forge = GiteaForge(
        "https://git.example/o/r", "write-tok",
        transport=httpx.MockTransport(handler),
    )
    result = run_coro(forge.apply_default_branch_protection("main"))
    assert result.outcome == "applied"
    assert writes, "expected a protection write"
    contexts = writes[0].get("status_check_contexts")
    assert contexts == ["ci"]
    assert "*" not in contexts
    assert "*pipeline*" not in (result.shape.required_status_checks or [])
