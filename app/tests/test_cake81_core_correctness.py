"""CAKE-81 — small app-core correctness holes (atomic yaml, wipe fence,
packed-refs last-good, http clone_user, connection_status honesty,
redaction key builders).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

import pytest
import yaml

from devcake.domain.run import Run, is_pre_wipe


_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run_coro(c):
    return _LOOP.run_until_complete(c)


# ── Slice A: atomic YAML temp cleanup ───────────────────────────────────────

def test_atomic_yaml_unlinks_tmp_on_dump_failure(tmp_path):
    """_atomic_yaml must not leave .tmp behind when dump/fsync fails —
    same contract as secrets._atomic_write_bytes."""
    from devcake import config as cfg

    target = tmp_path / "config.yaml"
    before = set(tmp_path.iterdir())

    with mock.patch.object(yaml, "safe_dump", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            cfg._atomic_yaml(target, {"schema_version": 4})

    leftover = [p for p in tmp_path.iterdir() if p not in before]
    assert leftover == [], f"temp left behind after failed atomic write: {leftover}"
    assert not target.exists()


# ── Slice B: shared wipe-fence fallback ─────────────────────────────────────

class _BareStore:
    """Minimal store with wipe_generation only — no is_current_generation."""

    def __init__(self, wipe_generation: int = 0) -> None:
        self.wipe_generation = wipe_generation


def test_is_pre_wipe_fallback_agrees_without_is_current_generation():
    """Both former _pre_wipe copies must agree: wipe_gen<=0 → current;
    gen != wipe_gen → pre-wipe. Shared helper is the single fallback."""
    run = Run(run_id="r1", mission_key="M", mission_type="EXECUTE",
              mission_pmo_id="1", pmo_ref="lin", dev_type="implementer",
              seq=1, store_gen=0)

    assert is_pre_wipe(_BareStore(0), run) is False
    assert is_pre_wipe(_BareStore(1), run) is True  # gen 0 != wipe 1

    run.store_gen = 1
    assert is_pre_wipe(_BareStore(1), run) is False  # exact match
    assert is_pre_wipe(_BareStore(2), run) is True   # gen != wipe


def test_is_pre_wipe_prefers_store_is_current_generation():
    class Store:
        wipe_generation = 99  # ignored when is_current_generation exists

        def is_current_generation(self, run: Run) -> bool:
            return run.store_gen == 7

    run = Run(run_id="r1", mission_key="M", mission_type="EXECUTE",
              mission_pmo_id="1", pmo_ref="lin", dev_type="implementer",
              seq=1, store_gen=7)
    assert is_pre_wipe(Store(), run) is False
    run.store_gen = 0
    assert is_pre_wipe(Store(), run) is True


def test_runs_and_finalize_pre_wipe_share_helper():
    """Drift pin: RunManager._pre_wipe and finalize._pre_wipe both call
    is_pre_wipe so fallback arithmetic cannot diverge."""
    from devcake.domain.runs import RunManager
    from devcake.domain.orchestrator import finalize

    store = _BareStore(1)
    run = Run(run_id="r1", mission_key="M", mission_type="EXECUTE",
              mission_pmo_id="1", pmo_ref="lin", dev_type="implementer",
              seq=1, store_gen=0)

    class _Mgr:
        def __init__(self):
            self.store = store

    class _Outer:
        def __init__(self):
            self.runs = _Mgr()

    rm = RunManager.__new__(RunManager)
    rm.store = store
    assert rm._pre_wipe(run) is True
    assert finalize._pre_wipe(_Outer(), run) is True
    assert rm._pre_wipe(run) == finalize._pre_wipe(_Outer(), run) == is_pre_wipe(store, run)


# ── Slice C: has_last_good after git gc (packed-refs) ────────────────────────

def test_has_last_good_true_when_only_packed_refs(tmp_path):
    """After `git gc` packs refs and prunes loose refs/heads/*, last-good
    content must still read as present."""
    from test_repo_mirror import make_cache, R1

    cache, _, _ = make_cache(tmp_path, [R1])
    p = cache.mirror_path("alpha")
    p.mkdir(parents=True)
    (p / "refs" / "heads").mkdir(parents=True)
    # packed-refs only — no loose heads (post-gc shape)
    (p / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        f"{'a' * 40} refs/heads/main\n"
    )
    assert cache.has_last_good("alpha") is True


def test_has_last_good_false_for_empty_init(tmp_path):
    from test_repo_mirror import make_cache, R1

    cache, _, _ = make_cache(tmp_path, [R1])
    p = cache.mirror_path("alpha")
    p.mkdir(parents=True)
    (p / "refs" / "heads").mkdir(parents=True)
    (p / "HEAD").write_text("ref: refs/heads/main\n")
    # empty packed-refs (or absent) + empty heads → never-fetched init
    (p / "packed-refs").write_text("# pack-refs with: peeled fully-peeled sorted\n")
    assert cache.has_last_good("alpha") is False


# ── Slice D: sync_one clone_user for http:// ─────────────────────────────────

def test_sync_one_embeds_clone_user_for_http_urls(tmp_path):
    """sync_one must insert clone_user for any scheme the way remote_head does
    — https-only str.replace leaves http:// remotes userless."""
    from test_repo_mirror import make_cache, Repo

    http_repo = Repo(name="alpha", url="http://gitea.local/o/alpha.git",
                     fake_token_ro="ro-a", fake_token="rw-a")
    cache, calls, _ = make_cache(tmp_path, [http_repo])
    st = run_coro(cache.sync_one("alpha"))
    assert st.ok
    add = next(c for c in calls if c[:1] == ["-C"] and "remote" in c and "add" in c)
    # remote add origin <url>
    url = add[-1]
    assert url.startswith("http://oauth2@gitea.local/"), url


# ── Slice G: connection_status per-field honesty ─────────────────────────────

def test_connection_status_does_not_bump_sibling_updated_at(tmp_path, monkeypatch):
    """Writing token_ro must not present a newer updated_at for sibling
    fields that share the connection file."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as s

    s.write_connection_secret("repo", "main", "token", "tok-write-value-aa")
    before = s.connection_status("repo", "main", "token")
    assert before["present"] is True
    # Sole present field may report file mtime (honest for that field).
    assert before["updated_at"] is not None
    assert s.connection_status("repo", "main", "token_ro")["present"] is False

    s.write_connection_secret("repo", "main", "token_ro", "tok-ro-value-bbbb")
    token_st = s.connection_status("repo", "main", "token")
    ro_st = s.connection_status("repo", "main", "token_ro")
    assert token_st["present"] is True
    assert ro_st["present"] is True
    # Multi-field: shared-file mtime is not each field's update time.
    assert token_st["updated_at"] is None
    assert ro_st["updated_at"] is None


# ── Slice H: redaction registration key builders ─────────────────────────────

def test_redaction_key_builders_match_register_schemes():
    """conn / harness / cred key strings must be identical across write and
    boot-register paths — one helper each, not parallel f-strings."""
    from devcake import secrets as s

    assert s.conn_redact_key("repo", "main", "token") == "conn:repo:main:token"
    assert s.harness_redact_key("XAI_API_KEY") == "harness:XAI_API_KEY"
    assert s.cred_redact_key("main-dev", "grok-auth.json") == "cred:main-dev:grok-auth.json"
