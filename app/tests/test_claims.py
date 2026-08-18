"""`.claims/` conveyor (ADR-0035 D4/D8) — public seam: append_from_harvest,
prune_board, prune_all, claim_id. Fake notebooks; independent expected values."""

from __future__ import annotations

import asyncio
import json

from devcake.domain import claims
from devcake.domain.run import Run
from devcake.config import AppConfig, Budgets


def run_coro(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


class FakeNotebooks:
    def __init__(self, *, writable=None, files=None):
        self.writable = set(writable or [])
        # card -> {path: text}
        self.files: dict[str, dict[str, str]] = files or {}
        self.commits: list[tuple] = []
        self.fail_commit = False

    def can_write(self, card):
        return card in self.writable

    async def list_json_names(self, card):
        if card not in self.files and card not in self.writable:
            return None
        return [p.split("/")[-1] for p in self.files.get(card, {})
                if p.endswith(".json")]

    async def list_claim_meta(self, card):
        names = await self.list_json_names(card)
        if names is None:
            return None
        out = []
        for fname in names:
            raw = self.files.get(card, {}).get(f".claims/{fname}", "{}")
            rec = json.loads(raw)
            out.append({"id": rec.get("id") or fname[:-5],
                        "source_instance": rec.get("source_instance", ""),
                        "source_pmo_id": rec.get("source_pmo_id", "")})
        return out

    async def has_readme(self, card):
        if card not in self.files and card not in self.writable:
            return None
        return ".claims/README.md" in self.files.get(card, {})

    async def snapshot(self, card):
        names = await self.list_json_names(card)
        if names is None:
            return None
        return {"json_names": names,
                "has_readme": ".claims/README.md" in self.files.get(card, {})}

    async def commit(self, card, *, creates, deletes, message):
        if self.fail_commit:
            raise RuntimeError("forge down")
        for path in creates:
            assert path.startswith(".claims/"), path
        for path in deletes:
            assert path.startswith(".claims/"), path
            assert not path.endswith("README.md")
        store = self.files.setdefault(card, {})
        store.update(creates)
        for path in deletes:
            store.pop(path, None)
        self.commits.append((card, list(creates), list(deletes), message))


def _run(**kw):
    defaults = dict(
        run_id="ENG-T-1-1-EXECUTE-AAAAAA", mission_key="T-1",
        mission_type="EXECUTE", repo_ref="webapp", pmo_ref="eng",
        mission_pmo_id="pmo-1", dev_type="implementer", seq=2,
        memory_mounts=[{"card": "nb", "binding": "board", "commit": "abc",
                        "stale_cache": False, "path": "/workspace/memory/nb"}],
    )
    defaults.update(kw)
    return Run(**defaults)


def test_claim_id_is_deterministic_and_path_safe():
    a = claims.claim_id("eng", "pmo-1", 2, 0)
    b = claims.claim_id("eng", "pmo-1", 2, 0)
    c = claims.claim_id("eng", "pmo-1", 2, 1)
    assert a == b and a != c
    assert claims._SAFE_ID.match(a)
    # N6: two boards with colliding pmo ids never dedup each other
    assert claims.claim_id("cs", "pmo-1", 2, 0) != a


def test_append_creates_readme_once_and_one_file_per_entry():
    nb = FakeNotebooks(writable={"nb"})
    cfg = AppConfig(budgets=Budgets(claims_queue_max=50))
    entries = [
        {"finding": "f1", "evidence": "e1", "scope": "s1"},
        {"finding": "f2", "evidence": "e2", "scope": "s2"},
    ]
    got = run_coro(claims.append_from_harvest(nb, cfg, _run(), entries))
    assert got == {"nb": 2}
    files = nb.files["nb"]
    assert files[".claims/README.md"] == claims.CLAIMS_README
    jsons = [p for p in files if p.endswith(".json")]
    assert len(jsons) == 2
    rec = json.loads(files[jsons[0]])
    assert rec["schema"] == "devcake-claims/v1"
    assert rec["source_instance"] == "eng"
    assert rec["source_pmo_id"] == "pmo-1"
    assert rec["step"] == 2
    assert rec["run_id"].endswith("AAAAAA")
    assert "rationale" not in rec and "status" not in rec
    # second harvest is idempotent (file exists)
    got2 = run_coro(claims.append_from_harvest(nb, cfg, _run(), entries))
    assert got2 == {}
    assert len([p for p in nb.files["nb"] if p.endswith(".json")]) == 2
    # README not rewritten
    nb.files["nb"][".claims/README.md"] = "operator edited"
    run_coro(claims.append_from_harvest(
        nb, cfg, _run(seq=3, mission_pmo_id="pmo-2"),
        [{"finding": "f3", "evidence": "e3", "scope": "s3"}]))
    assert nb.files["nb"][".claims/README.md"] == "operator edited"


def test_append_skips_own_repo_ref_and_empty_snapshot():
    nb = FakeNotebooks(writable={"nb"})
    cfg = AppConfig()
    curator = _run(repo_ref="nb", memory_mounts=[
        {"card": "nb", "binding": "board"}])
    assert run_coro(claims.append_from_harvest(
        nb, cfg, curator,
        [{"finding": "f", "evidence": "e", "scope": "s"}])) == {}
    assert run_coro(claims.append_from_harvest(
        nb, cfg, _run(memory_mounts=[]),
        [{"finding": "f", "evidence": "e", "scope": "s"}])) == {}


def test_cap_refuses_new_does_not_evict():
    existing_id = claims.claim_id("old", "old", 1, 0)
    nb = FakeNotebooks(
        writable={"nb"},
        files={"nb": {f".claims/{existing_id}.json": json.dumps({
            "id": existing_id, "source_instance": "old",
            "source_pmo_id": "old"})}})
    cfg = AppConfig(budgets=Budgets(claims_queue_max=1))
    claims.claims_queue_capped.clear()
    got = run_coro(claims.append_from_harvest(
        nb, cfg, _run(),
        [{"finding": "f", "evidence": "e", "scope": "s"}]))
    assert got == {}
    assert "nb" in claims.claims_queue_capped
    assert existing_id + ".json" in str(nb.files["nb"])


def test_write_failure_does_not_raise():
    nb = FakeNotebooks(writable={"nb"})
    nb.fail_commit = True
    cfg = AppConfig()
    got = run_coro(claims.append_from_harvest(
        nb, cfg, _run(),
        [{"finding": "f", "evidence": "e", "scope": "s"}]))
    assert got == {}


def test_prune_deletes_matching_source_leaves_readme_and_other_boards():
    id_a = claims.claim_id("eng", "pmo-1", 2, 0)
    id_b = claims.claim_id("cs", "other", 1, 0)
    nb = FakeNotebooks(
        writable={"nb"},
        files={"nb": {
            ".claims/README.md": claims.CLAIMS_README,
            f".claims/{id_a}.json": json.dumps({
                "id": id_a, "source_instance": "eng",
                "source_pmo_id": "pmo-1"}),
            f".claims/{id_b}.json": json.dumps({
                "id": id_b, "source_instance": "cs",
                "source_pmo_id": "pmo-9"}),
        }})
    got = run_coro(claims.prune_board(
        nb, ["nb"], source_instance="eng", source_pmo_id="pmo-1"))
    assert got == {"nb": 1}
    assert f".claims/{id_a}.json" not in nb.files["nb"]
    assert f".claims/{id_b}.json" in nb.files["nb"]
    assert ".claims/README.md" in nb.files["nb"]


def test_prune_board_and_filters_when_both_source_fields_set():
    """N6: colliding numeric pmo ids across boards must not cross-delete.

    When both source_instance and source_pmo_id are provided, each non-empty
    filter must match (AND). An OR match would delete the other board's claim
    that only shares the pmo id — the same collision N6 salted claim_id for.
    """
    id_target = claims.claim_id("eng", "pmo-1", 2, 0)
    id_same_pmo_other_board = claims.claim_id("cs", "pmo-1", 1, 0)
    id_same_board_other_mission = claims.claim_id("eng", "pmo-2", 1, 0)
    nb = FakeNotebooks(
        writable={"nb"},
        files={"nb": {
            ".claims/README.md": claims.CLAIMS_README,
            f".claims/{id_target}.json": json.dumps({
                "id": id_target, "source_instance": "eng",
                "source_pmo_id": "pmo-1"}),
            f".claims/{id_same_pmo_other_board}.json": json.dumps({
                "id": id_same_pmo_other_board, "source_instance": "cs",
                "source_pmo_id": "pmo-1"}),
            f".claims/{id_same_board_other_mission}.json": json.dumps({
                "id": id_same_board_other_mission, "source_instance": "eng",
                "source_pmo_id": "pmo-2"}),
        }})
    got = run_coro(claims.prune_board(
        nb, ["nb"], source_instance="eng", source_pmo_id="pmo-1"))
    assert got == {"nb": 1}
    assert f".claims/{id_target}.json" not in nb.files["nb"]
    assert f".claims/{id_same_pmo_other_board}.json" in nb.files["nb"]
    assert f".claims/{id_same_board_other_mission}.json" in nb.files["nb"]


def test_prune_board_instance_only_wipes_all_missions_on_that_board():
    """Board-level prune (no source_pmo_id): every claim from that instance."""
    id_a = claims.claim_id("eng", "pmo-1", 2, 0)
    id_b = claims.claim_id("eng", "pmo-2", 1, 0)
    id_other = claims.claim_id("cs", "pmo-1", 1, 0)
    nb = FakeNotebooks(
        writable={"nb"},
        files={"nb": {
            f".claims/{id_a}.json": json.dumps({
                "id": id_a, "source_instance": "eng",
                "source_pmo_id": "pmo-1"}),
            f".claims/{id_b}.json": json.dumps({
                "id": id_b, "source_instance": "eng",
                "source_pmo_id": "pmo-2"}),
            f".claims/{id_other}.json": json.dumps({
                "id": id_other, "source_instance": "cs",
                "source_pmo_id": "pmo-1"}),
        }})
    got = run_coro(claims.prune_board(
        nb, ["nb"], source_instance="eng"))
    assert got == {"nb": 2}
    assert f".claims/{id_a}.json" not in nb.files["nb"]
    assert f".claims/{id_b}.json" not in nb.files["nb"]
    assert f".claims/{id_other}.json" in nb.files["nb"]


def test_two_file_creates_do_not_share_a_path():
    """Drain-delete vs concurrent create cannot conflict: distinct files."""
    id0 = claims.claim_id("eng", "pmo-1", 2, 0)
    id1 = claims.claim_id("eng", "pmo-1", 2, 1)
    assert id0 != id1
    assert f".claims/{id0}.json" != f".claims/{id1}.json"


def test_prune_all_removes_orphans_and_keeps_readme():
    """N7: clear-all prunes EVERY claim file — a claim from a board that
    was deleted from config before the Clear must not leak forever."""
    id_a = claims.claim_id("eng", "pmo-1", 2, 0)
    id_orphan = claims.claim_id("gone", "pmo-9", 1, 0)
    nb = FakeNotebooks(
        writable={"nb"},
        files={"nb": {
            ".claims/README.md": claims.CLAIMS_README,
            f".claims/{id_a}.json": json.dumps({
                "id": id_a, "source_instance": "eng",
                "source_pmo_id": "pmo-1"}),
            f".claims/{id_orphan}.json": json.dumps({
                "id": id_orphan, "source_instance": "gone",
                "source_pmo_id": "pmo-9"}),
        }})
    claims.claims_queue_capped.add("nb")
    got = run_coro(claims.prune_all(nb, ["nb"]))
    assert got == {"nb": 2}
    assert list(nb.files["nb"]) == [".claims/README.md"]
    assert claims.claims_depth["nb"] == 0
    # Clear emptied the queue — standing cap alert must not outlive it.
    assert "nb" not in claims.claims_queue_capped


def test_prune_all_empty_queue_zeros_depth_and_clears_capped():
    """Empty on disk after Clear: still refresh advisory depth + cap flag."""
    nb = FakeNotebooks(
        writable={"nb"},
        files={"nb": {".claims/README.md": claims.CLAIMS_README}})
    claims.claims_depth["nb"] = 7
    claims.claims_queue_capped.add("nb")
    got = run_coro(claims.prune_all(nb, ["nb"]))
    assert got == {}
    assert claims.claims_depth["nb"] == 0
    assert "nb" not in claims.claims_queue_capped
    assert nb.files["nb"] == {".claims/README.md": claims.CLAIMS_README}


def test_append_clears_capped_when_listing_shows_room_and_nothing_to_write():
    """Curator drain can free the queue without an app write.

    Next harvest that lists under-cap depth must drop the standing
    claims_queue_capped warning even when every entry is a file-exists dedup
    (no commit) — otherwise /health keeps claiming leads are refused.
    """
    existing_id = claims.claim_id("eng", "pmo-1", 2, 0)
    nb = FakeNotebooks(
        writable={"nb"},
        files={"nb": {
            ".claims/README.md": claims.CLAIMS_README,
            f".claims/{existing_id}.json": json.dumps({
                "id": existing_id, "source_instance": "eng",
                "source_pmo_id": "pmo-1"}),
        }})
    cfg = AppConfig(budgets=Budgets(claims_queue_max=50))
    claims.claims_queue_capped.add("nb")
    claims.claims_depth["nb"] = 50  # stale post-drain
    got = run_coro(claims.append_from_harvest(
        nb, cfg, _run(),
        [{"finding": "f", "evidence": "e", "scope": "s"}]))
    assert got == {}
    assert claims.claims_depth["nb"] == 1
    assert "nb" not in claims.claims_queue_capped
