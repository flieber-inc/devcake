"""The CAKE-48 clobber (observed live 2026-08-17): finalize's stage-label add
and the discovery sweep's gate retire hit the same mission in the same second,
and the sweep's full-set rewrite — computed from a read taken before the add —
deleted the fresh stage label, stranding the mission as "in_progress without
stage label". Two guards close it: label mutations are serialized per mission
(`adapters/_toolkit.label_write_lock`), and the Linear issue path uses
per-label mutations so no full-set rewrite exists to clobber at all."""
import asyncio

from devcake.adapters import _toolkit
from devcake.adapters._toolkit import label_write_lock
from devcake.adapters.gitea_issues.adapter import GiteaIssuesAdapter
from devcake.adapters.github_issues.adapter import GitHubIssuesAdapter
from devcake.adapters.gitlab_issues.adapter import GitLabIssuesAdapter
from devcake.adapters.linear.adapter import LinearAdapter
from devcake.domain.model import MissionRef


def run_coro(c):
    return asyncio.get_event_loop().run_until_complete(c)


# --- the lock itself -------------------------------------------------------

def test_lock_serializes_same_mission():
    order = []

    async def worker(name):
        async with label_write_lock("m1"):
            order.append(f"{name}:in")
            await asyncio.sleep(0)     # a second task could interleave here
            order.append(f"{name}:out")

    run_coro(asyncio.gather(worker("a"), worker("b")))
    assert order == ["a:in", "a:out", "b:in", "b:out"]


def test_lock_does_not_couple_missions():
    order = []

    async def worker(pmo_id):
        async with label_write_lock(pmo_id):
            order.append(f"{pmo_id}:in")
            await asyncio.sleep(0)
            order.append(f"{pmo_id}:out")

    run_coro(asyncio.gather(worker("m1"), worker("m2")))
    assert order == ["m1:in", "m2:in", "m1:out", "m2:out"]


def test_lock_registry_prunes_unheld():
    async def churn():
        for i in range(_toolkit._LABEL_LOCKS_MAX + 50):
            async with label_write_lock(f"churn-{i}"):
                pass

    run_coro(churn())
    assert len(_toolkit._LABEL_LOCKS) <= _toolkit._LABEL_LOCKS_MAX + 1


def test_eviction_does_not_orphan_a_waiter():
    """Registry eviction must not drop a lock a waiter still references.

    ``asyncio.Lock.locked()`` is False as soon as the holder releases, even
    when another task is waiting on that same Lock. Deleting the registry
    entry then lets a later lookup install a fresh Lock while the waiter
    still serializes on the old one — two writers in one mission's critical
    section (CAKE-48-class clobber). Idle entries with zero in-flight
    entrants must still prune (see ``test_lock_registry_prunes_unheld``).
    """
    victim = "victim-orphan-waiter"
    _toolkit._LABEL_LOCKS.clear()
    _toolkit._LABEL_LOCK_OCCUPANCY.clear()

    async def scenario():
        for i in range(_toolkit._LABEL_LOCKS_MAX):
            _toolkit._LABEL_LOCKS[f"filler-{i}"] = asyncio.Lock()

        held = asyncio.Event()
        release_holder = asyncio.Event()
        eviction_done = asyncio.Event()
        waiter_in_cs = asyncio.Event()
        overlap = {"cur": 0, "max": 0}
        locks = {}

        async def holder():
            async with label_write_lock(victim):
                locks["original"] = _toolkit._label_lock(victim)
                held.set()
                await release_holder.wait()
            # Just released: locked() is False while the waiter still
            # references this Lock. Prune before yielding so the waiter
            # cannot re-acquire first (the CAKE-74 window).
            while len(_toolkit._LABEL_LOCKS) < _toolkit._LABEL_LOCKS_MAX:
                _toolkit._label_lock(f"pressure-{len(_toolkit._LABEL_LOCKS)}")
            _toolkit._label_lock("pressure-trigger")
            locks["after"] = _toolkit._label_lock(victim)
            eviction_done.set()

        async def waiter():
            await held.wait()
            async with label_write_lock(victim):
                overlap["cur"] += 1
                overlap["max"] = max(overlap["max"], overlap["cur"])
                waiter_in_cs.set()
                await asyncio.sleep(0.05)
                overlap["cur"] -= 1

        async def third():
            await eviction_done.wait()
            await waiter_in_cs.wait()
            async with label_write_lock(victim):
                overlap["cur"] += 1
                overlap["max"] = max(overlap["max"], overlap["cur"])
                await asyncio.sleep(0)
                overlap["cur"] -= 1

        h = asyncio.create_task(holder())
        await held.wait()
        w = asyncio.create_task(waiter())
        for _ in range(10):
            await asyncio.sleep(0)
        t = asyncio.create_task(third())
        release_holder.set()
        await asyncio.gather(h, w, t)

        assert locks["after"] is locks["original"], (
            "eviction replaced the mission lock while a waiter still "
            "referenced it")
        assert overlap["max"] == 1, (
            f"concurrent critical sections on the same mission "
            f"(max={overlap['max']})")

    run_coro(scenario())


# --- every adapter's swap holds the lock during its read -------------------
# The first awaited call inside each critical section asserts the mission's
# lock is held, then short-circuits — no further vendor fakes needed. Red
# without the fix: the lock is simply never acquired.

class _Seen(Exception):
    pass


def _assert_locked_then_stop(pmo_id):
    assert _toolkit._label_lock(pmo_id).locked(), (
        "swap read ran without the per-mission label lock")
    raise _Seen


def _swap_raises_seen(coro):
    try:
        run_coro(coro)
    except _Seen:
        return True
    raise AssertionError("swap returned before reaching its read seam")


def test_linear_issue_swap_holds_the_lock():
    ad = LinearAdapter("key")

    async def _gql(query, variables=None):
        _assert_locked_then_stop("i-lock")

    ad._gql = _gql
    assert _swap_raises_seen(
        ad.swap_labels(MissionRef("i-lock", "issue"), set(), {"X"}))


def test_linear_project_swap_holds_the_lock():
    ad = LinearAdapter("key")

    async def _all_project_labels():
        _assert_locked_then_stop("p-lock")

    ad._all_project_labels = _all_project_labels
    assert _swap_raises_seen(
        ad.swap_labels(MissionRef("p-lock", "project"), set(), {"X"}))


def test_gitea_swap_holds_the_lock():
    ad = GiteaIssuesAdapter("http://gitea:3000", "tok", "o/r")

    async def _ensure_label_cache():
        _assert_locked_then_stop("7")

    ad._ensure_label_cache = _ensure_label_cache
    assert _swap_raises_seen(
        ad.swap_labels(MissionRef("7", "issue"), set(), {"X"}))


def test_github_swap_holds_the_lock():
    ad = GitHubIssuesAdapter(None, "tok", "o/r")

    async def ensure_labels(team_ref, names):
        _assert_locked_then_stop("8")

    ad.ensure_labels = ensure_labels
    assert _swap_raises_seen(
        ad.swap_labels(MissionRef("8", "issue"), set(), {"X"}))


def test_gitlab_swap_holds_the_lock():
    ad = GitLabIssuesAdapter(None, "tok", "grp/proj")

    async def ensure_labels(team_ref, names):
        _assert_locked_then_stop("9")

    ad.ensure_labels = ensure_labels
    assert _swap_raises_seen(
        ad.swap_labels(MissionRef("9", "issue"), set(), {"X"}))


# --- the CAKE-48 shape, end to end on the Linear issue path ----------------

def test_concurrent_add_and_retire_cannot_clobber_the_stage_label():
    """Replay the race: one swap adds DEVCAKE-EXECUTE while another retires
    DEVCAKE-DISCOVERY, every fake call yielding control so the interleaving
    the live incident hit is on the table. Whatever the schedule, the stage
    label must survive. Red on the rewrite path: the retire's stale full-set
    write lands without EXECUTE and deletes it."""
    team = {n: f"id-{n}" for n in
            ("DEVCAKE", "DEVCAKE-CREATED", "DEVCAKE-DISCOVERY",
             "DEVCAKE-EXECUTE")}
    issue_labels = {"DEVCAKE", "DEVCAKE-CREATED", "DEVCAKE-DISCOVERY"}
    ad = LinearAdapter("key")

    async def _gql(query, variables=None):
        await asyncio.sleep(0)          # open the interleave window
        v = dict(variables or {})
        if "issueRemoveLabel" in query:
            by_id = {i: n for n, i in team.items()}
            issue_labels.discard(by_id[v["lid"]])
            return {"issueRemoveLabel": {"success": True}}
        if "issueAddLabel" in query:
            by_id = {i: n for n, i in team.items()}
            issue_labels.add(by_id[v["lid"]])
            return {"issueAddLabel": {"success": True}}
        if "issueUpdate" in query:      # the old rewrite, applied faithfully
            by_id = {i: n for n, i in team.items()}
            issue_labels.clear()
            issue_labels.update(by_id[i] for i in v["labelIds"])
            return {"issueUpdate": {"success": True}}
        return {"issue": {
            "team": {"key": "T"},
            "labels": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                       "nodes": [{"id": team[n], "name": n}
                                 for n in sorted(issue_labels)]}}}

    async def _team(_key):
        return {"id": "t1", "key": "T",
                "labels": {"nodes": [{"id": i, "name": n}
                                     for n, i in team.items()]}}

    ad._gql = _gql
    ad._team = _team
    ref = MissionRef("cake-48", "issue")
    run_coro(asyncio.gather(
        ad.swap_labels(ref, remove=set(), add={"DEVCAKE-EXECUTE"}),
        ad.swap_labels(ref, remove={"DEVCAKE-DISCOVERY"}, add=set())))
    assert "DEVCAKE-EXECUTE" in issue_labels
    assert "DEVCAKE-DISCOVERY" not in issue_labels
