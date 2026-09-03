"""Vendor header/body → RateSignal mappers, and the governed chokepoint of
every PMO adapter (ADR-0040)."""

import asyncio
import json

import httpx
import pytest

from devcake.adapters import budget as B
from devcake.adapters.gitea_issues.adapter import GiteaIssuesAdapter
from devcake.adapters.gitea_issues import adapter as gitea_mod
from devcake.adapters.github_issues.adapter import GitHubIssuesAdapter
from devcake.adapters.github_issues import adapter as github_mod
from devcake.adapters.gitlab_issues.adapter import GitLabIssuesAdapter
from devcake.adapters.gitlab_issues import adapter as gitlab_mod
from devcake.adapters.linear.adapter import LinearAdapter
from devcake.adapters.linear import adapter as linear_mod
from devcake.ports import pmo as pmo_port
from devcake.ports.pmo import PMOBudgetExceeded, PMOTransient, pmo_call


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Linear ───────────────────────────────────────────────────────────────────

LINEAR_HEADERS = {
    "x-ratelimit-requests-limit": "2500",
    "x-ratelimit-requests-remaining": "1234",
    "x-ratelimit-requests-reset": "1700000000000",       # epoch MILLISECONDS
    "x-ratelimit-complexity-limit": "3000000",
    "x-ratelimit-complexity-remaining": "1500000",
    "x-complexity": "1310",
    "x-ratelimit-endpoint-requests-limit": "100",
    "x-ratelimit-endpoint-requests-remaining": "90",
    "x-ratelimit-endpoint-requests-reset": "1700000000000",
    "x-ratelimit-endpoint-name": "issueCreate",
}


def test_linear_headers_map_to_the_signal():
    sig = linear_mod.rate_signal(httpx.Response(200, headers=LINEAR_HEADERS, json={"data": {}}))
    assert (sig.limit, sig.remaining) == (2500, 1234)
    assert sig.reset_at == pytest.approx(1700000000.0)   # ms → s
    assert sig.window_s == 3600 and sig.limited is False
    assert sig.complexity_fraction == pytest.approx(0.5)
    assert sig.endpoint == {"name": "issueCreate", "limit": 100, "remaining": 90}


def test_linear_ratelimited_body_is_a_rejection_with_the_refill_hint():
    body = {"errors": [{"message": "Rate limit exceeded",
                        "extensions": {"code": "RATELIMITED", "statusCode": 429,
                                       "meta": {"rateLimitResult": {
                                           "allowed": False, "remaining": 0,
                                           "limit": 2500, "duration": 3600000}}}}]}
    sig = linear_mod.rate_signal(httpx.Response(400, json=body))
    assert sig.limited is True
    assert sig.retry_after_s == pytest.approx(3600000 / 2500 / 1000 + 1)
    assert (sig.limit, sig.remaining) == (2500, 0)


def test_linear_bare_429_is_a_rejection():
    sig = linear_mod.rate_signal(httpx.Response(429, text="Too Many Requests",
                                                headers={"retry-after": "3"}))
    assert sig.limited is True and sig.retry_after_s == 3.0


def test_linear_non_json_4xx_is_a_permanent_error_not_a_decode_leak():
    pmo = LinearAdapter("k", transport=httpx.MockTransport(
        lambda r: httpx.Response(400, text="<html>bad gateway</html>")))
    with pytest.raises(RuntimeError, match="linear http 400"):
        run(pmo._gql("{ viewer { id } }"))


def test_linear_binds_the_budget_to_the_viewer():
    def handler(req):
        return httpx.Response(200, headers=LINEAR_HEADERS, json={"data": {
            "viewer": {"id": "user-42"},
            "teams": {"nodes": [{"id": "t1", "key": "DEV",
                                 "states": {"nodes": []}}]},
            "team": {"labels": {"nodes": [], "pageInfo": {"hasNextPage": False}}},
        }})
    pmo = LinearAdapter("k-one", transport=httpx.MockTransport(handler))
    twin = LinearAdapter("k-two", transport=httpx.MockTransport(handler))
    run(pmo._team("DEV"))
    run(twin._team("DEV"))
    assert pmo._budget is twin._budget          # two keys, one user, one bucket
    assert pmo._budget.principal == "user-42"
    assert pmo._budget.remaining == 1234


# ── GitHub Issues ────────────────────────────────────────────────────────────

def test_github_primary_headers_and_403_rate_limit():
    sig = github_mod.rate_signal(httpx.Response(200, headers={
        "x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4990",
        "x-ratelimit-reset": "1700000000", "x-ratelimit-resource": "core"},
        json={}))
    assert (sig.limit, sig.remaining, sig.reset_at) == (5000, 4990, 1700000000.0)
    assert sig.window_s == 3600 and not sig.limited
    sig = github_mod.rate_signal(httpx.Response(403, text="API rate limit exceeded"))
    assert sig.limited and sig.retry_after_s is None


def test_github_secondary_limit_waits_at_least_a_minute():
    sig = github_mod.rate_signal(httpx.Response(
        403, headers={"retry-after": "30"},
        text="You have exceeded a secondary rate limit"))
    assert sig.limited and sig.retry_after_s == 60.0


def test_github_non_core_resource_headers_are_ignored():
    sig = github_mod.rate_signal(httpx.Response(200, headers={
        "x-ratelimit-limit": "30", "x-ratelimit-remaining": "0",
        "x-ratelimit-resource": "search"}, json={}))
    assert sig.limit is None and sig.remaining is None


# ── GitLab / Gitea ───────────────────────────────────────────────────────────

def test_gitlab_headers_map_to_a_per_minute_window():
    sig = gitlab_mod.rate_signal(httpx.Response(429, headers={
        "ratelimit-limit": "2000", "ratelimit-remaining": "0",
        "ratelimit-reset": "1700000060", "retry-after": "17"}, text=""))
    assert (sig.limit, sig.remaining, sig.reset_at) == (2000, 0, 1700000060.0)
    assert sig.window_s == 60 and sig.limited and sig.retry_after_s == 17.0


def test_gitea_has_no_headers_until_a_proxy_rejects():
    assert gitea_mod.rate_signal(httpx.Response(200, json={})) == B.RateSignal()
    sig = gitea_mod.rate_signal(httpx.Response(429, headers={"retry-after": "5"}))
    assert sig.limited and sig.retry_after_s == 5.0


# ── the governed chokepoint, every adapter ───────────────────────────────────

def _adapters(transport):
    return {
        "linear": (LinearAdapter("k", transport=transport),
                   lambda a: a._gql("{ viewer { id } }")),
        "github_issues": (GitHubIssuesAdapter("https://api.github.com", "tok", "o/r",
                                              transport=transport),
                          lambda a: a._req("GET", "/x")),
        "gitlab_issues": (GitLabIssuesAdapter("https://gitlab.example", "tok", "g/p",
                                              transport=transport),
                          lambda a: a._req("GET", "/x")),
        "gitea_issues": (GiteaIssuesAdapter("http://gitea.example", "tok", "o/r",
                                            transport=transport),
                         lambda a: a._req("GET", "/x")),
    }


class Flaky:
    """429 once, then 200 with an empty JSON body."""

    def __init__(self):
        self.calls = 0

    def __call__(self, req):
        self.calls += 1
        if self.calls == 1:
            return httpx.Response(429, headers={"retry-after": "0.01"}, json={})
        return httpx.Response(200, json={"data": {}})


# GitHub treats any rejection carrying retry-after as a secondary limit and
# waits at least a minute (vendor guidance); the others honour the header.
EXPECTED_WAIT = {"github_issues": 60.0}


@pytest.mark.parametrize("system", sorted(_adapters(None)))
def test_critical_call_survives_one_rejection_routine_does_not(system, monkeypatch):
    clock = {"t": 1_000_000.0, "mono": 500.0}
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        clock["t"] += s
        clock["mono"] += s
    monkeypatch.setattr(B, "_sleep", fake_sleep)
    monkeypatch.setattr(B, "_clock", lambda: clock["t"])
    monkeypatch.setattr(B, "_mono", lambda: clock["mono"])
    monkeypatch.setattr(pmo_port, "_mono", lambda: clock["mono"])
    monkeypatch.setattr(B, "_OFF", False)

    flaky = Flaky()
    adapter, call = _adapters(httpx.MockTransport(flaky))[system]
    with pmo_call("critical"):
        run(call(adapter))
    assert flaky.calls == 2
    assert sleeps == [pytest.approx(EXPECTED_WAIT.get(system, 0.01))]

    B.reset()
    flaky = Flaky()
    adapter, call = _adapters(httpx.MockTransport(flaky))[system]
    with pytest.raises(PMOTransient) as ei:
        run(call(adapter))
    assert flaky.calls == 1 and not isinstance(ei.value, PMOBudgetExceeded)


@pytest.mark.parametrize("system", sorted(_adapters(None)))
def test_routine_call_is_refused_before_the_wire_when_the_reserve_is_reached(system, monkeypatch):
    monkeypatch.setattr(B, "_OFF", False)
    hits = []
    adapter, call = _adapters(httpx.MockTransport(
        lambda r: (hits.append(1), httpx.Response(200, json={"data": {}}))[1]))[system]
    adapter._budget.observe(B.RateSignal(limit=100, remaining=5,
                                         reset_at=B._clock() + 3600))
    with pytest.raises(PMOBudgetExceeded):
        run(call(adapter))
    assert hits == []


def test_linear_complexity_rejection_does_not_pose_as_the_request_quota():
    body = {"errors": [{"message": "Rate limit exceeded",
                        "extensions": {"code": "RATELIMITED",
                                       "meta": {"rateLimitResult": {
                                           "allowed": False, "remaining": 0,
                                           "limit": 3000000, "duration": 3600000}}}}]}
    sig = linear_mod.rate_signal(httpx.Response(400, json=body))
    assert sig.limited is True
    assert sig.limit is None and sig.remaining is None
    assert sig.retry_after_s == 5.0


def test_discovery_kick_runs_routine_even_inside_a_critical_context():
    """The harvest fires the kick from finalize's critical context; the
    spawned task must not inherit it for an enumeration read."""
    import asyncio
    from types import SimpleNamespace
    from devcake.domain.steward_service import StewardService
    from devcake.ports.pmo import pmo_call_ctx

    seen = []

    class FakePMO:
        async def list_all(self, team):
            seen.append(pmo_call_ctx.get().call_class)
            return []

    mgr = SimpleNamespace(pmo=FakePMO(), instance=SimpleNamespace(team_key="T"),
                          instance_name="i")
    svc = StewardService.__new__(StewardService)
    svc.mgr = mgr

    async def no_dispatch(missions):
        return None
    svc.maybe_dispatch_discovery = no_dispatch

    async def main():
        with pmo_call("critical"):
            svc.kick_discovery()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
    run(main())
    assert seen == ["routine"]
