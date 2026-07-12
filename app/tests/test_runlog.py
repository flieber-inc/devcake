"""Unit tests for the per-run live log store and the run.log ingress path."""

import asyncio
from pathlib import Path

from devcake.runlog import CAP_MARKER, MAX_LOG_BYTES, RunLogStore
from devcake.state import Run, RunStore
from devcake.runs import RunManager

RID = "T-1-1-EXECUTE-AAAAAA"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_append_read_roundtrip(tmp_path: Path):
    store = RunLogStore(root=tmp_path)
    store.append(RID, ["one", "two\n", "three"])
    seq, text = store.read(RID)
    assert seq == 3
    assert text == "one\ntwo\nthree\n"


def test_read_tail_and_missing(tmp_path: Path):
    store = RunLogStore(root=tmp_path)
    assert store.read("nope") == (0, "")
    store.append(RID, [f"l{i}" for i in range(5)])
    _seq, text = store.read(RID, tail=2)
    assert text == "l3\nl4\n"


def test_seq_lazy_init_from_existing_file(tmp_path: Path):
    RunLogStore(root=tmp_path).append(RID, ["a", "b"])
    fresh = RunLogStore(root=tmp_path)  # simulates app restart
    assert fresh.read(RID)[0] == 2
    fresh.append(RID, ["c"])
    assert fresh.read(RID) == (3, "a\nb\nc\n")


def test_subscribe_receives_seq_lines_and_close_sentinel(tmp_path: Path):
    async def scenario():
        store = RunLogStore(root=tmp_path)
        q = store.subscribe(RID)
        store.append(RID, ["hello"])
        assert await q.get() == (1, "hello")
        store.close(RID)
        assert await q.get() is None
        store.unsubscribe(RID, q)
        assert RID not in store._subs
    run(scenario())


def test_full_queue_drops_silently(tmp_path: Path):
    async def scenario():
        store = RunLogStore(root=tmp_path)
        q = store.subscribe(RID)
        for _ in range(3):
            store.append(RID, ["x"] * 500)   # 1500 > QUEUE_SIZE
        assert q.qsize() == 1000
        assert store.read(RID)[0] == 1500    # file got everything
    run(scenario())


def test_max_bytes_cap(tmp_path: Path):
    store = RunLogStore(root=tmp_path)
    store.path(RID).write_bytes(b"x" * MAX_LOG_BYTES)
    store.append(RID, ["over"])
    assert store.read(RID)[1].endswith(CAP_MARKER + "\n")
    size = store.path(RID).stat().st_size
    store.append(RID, ["more"])              # capped: no-op
    assert store.path(RID).stat().st_size == size


def test_clear_unlinks_and_closes(tmp_path: Path):
    async def scenario():
        store = RunLogStore(root=tmp_path)
        store.append(RID, ["a"])
        q = store.subscribe(RID)
        assert store.clear() == 1
        assert await q.get() is None
        assert store.read(RID) == (0, "")
    run(scenario())


def test_stream_replays_then_follows_no_dupes(tmp_path: Path):
    async def scenario():
        store = RunLogStore(root=tmp_path)
        store.append(RID, ["pre1", "pre2"])
        terminal = False
        events = []

        async def consume():
            async for chunk in store.stream(RID, lambda: terminal):
                events.append(chunk)

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.05)            # let it subscribe + replay
        store.append(RID, ["live3"])
        await asyncio.sleep(0.05)
        terminal = True
        store.close(RID)
        await asyncio.wait_for(task, timeout=5)
        datas = [e for e in events if e.startswith("data: ")]
        assert datas == ["data: pre1\n\n", "data: pre2\n\n", "data: live3\n\n"]
        assert events[-1] == "event: end\ndata: {}\n\n"
    run(scenario())


def test_stream_terminal_run_ends_immediately(tmp_path: Path):
    async def scenario():
        store = RunLogStore(root=tmp_path)
        store.append(RID, ["done already"])
        events = [c async for c in store.stream(RID, lambda: True)]
        assert events == ["data: done already\n\n", "event: end\ndata: {}\n\n"]
    run(scenario())


# ── ingress handler path (RunManager.handle "run.log") ───────────────────────

def make_manager(tmp_path: Path) -> RunManager:
    mgr = RunManager(RunStore(root=tmp_path / "runs"), messaging=None, executor=None)
    mgr.runlog = RunLogStore(root=tmp_path / "runlogs")
    mgr.store.save(Run(run_id=RID, mission_key="T-1", mission_type="EXECUTE",
                       dev_type="senior-dev", seq=1))
    return mgr


def test_handler_appends_and_redacts(tmp_path: Path):
    mgr = make_manager(tmp_path)
    token = "ghp_" + "a" * 30
    run(mgr.handle(RID, "run.log", {"lines": [f"pushing with {token}", "ok"]}))
    _seq, text = mgr.runlog.read(RID)
    assert token not in text
    assert "«REDACTED»" in text
    assert "ok" in text


def test_handler_oauth_payload_still_routes_to_oauth_mgr(tmp_path: Path):
    mgr = make_manager(tmp_path)
    seen = []
    mgr.oauth_mgr = type("Stub", (), {
        "on_log": lambda self, rid, p: seen.append((rid, p)),
        "sessions": {}})()
    run(mgr.handle(RID, "run.log", {"oauth_url": "https://x", "code": "AB-CD"}))
    assert seen == [(RID, {"oauth_url": "https://x", "code": "AB-CD"})]
    assert mgr.runlog.read(RID) == (0, "")   # no log file written


def test_finalize_closes_stream(tmp_path: Path):
    async def scenario():
        mgr = make_manager(tmp_path)
        # messaging only needed by _finalize's ACL cleanup
        class M:
            async def delete_run_user(self, rid): ...
            async def delete_reply_stream(self, rid): ...
        mgr.messaging = M()
        q = mgr.runlog.subscribe(RID)
        await mgr.handle(RID, "run.artifacts",
                         {"result": {"outcome": "executed", "summary": "s"}})
        assert q.get_nowait() is None        # sentinel delivered
    run(scenario())
