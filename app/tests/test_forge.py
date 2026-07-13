"""docs/06 §5 port contract, both v0 adapters: mergeable() tri-state signal
mapping, the transient-409 merge retry, and the normalized ForgeError type
(GitLab must never leak httpx exceptions). No network — _req is stubbed."""
import asyncio

import httpx
import pytest

from devcake.adapters.github.adapter import GitHubForge
from devcake.ports.forge import ForgeError
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
        raise ForgeError("conflicts", status=405)
    forge2._req = conflict_405
    with pytest.raises(ForgeError) as e:
        run_coro(forge2.merge(8))
    assert len(calls2) == 1 and e.value.status == 405   # real failure: no retry


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
