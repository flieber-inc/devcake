"""POST /connections/copy-secrets contract: slot-for-slot token copy.

All-or-nothing validation (the /secrets/clear precedent): any unknown or
incompatible target refuses the whole batch with nothing written. Values
never appear in a response — results carry field NAMES only.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from devcake.config import AppConfig, PMOInstance, RepoInstance


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Runtime:
    def __init__(self):
        self.cleared = []

    def clear_breaker(self, name):
        self.cleared.append(name)


def _rig(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.api import connections_service as cs

    cfg = AppConfig(
        repos=[
            RepoInstance(name="alpha", forge="github",
                         url="https://github.com/example-org/alpha"),
            RepoInstance(name="beta", forge="github",
                         url="https://github.com/example-org/beta"),
            RepoInstance(name="gamma", forge="gitlab",
                         url="https://gitlab.com/example-org/gamma"),
        ],
        pmos=[
            PMOInstance(name="ghboard", system="github_issues",
                        team_key="example-org/alpha"),
            PMOInstance(name="linboard", system="linear", team_key="DEV"),
            PMOInstance(name="linboard2", system="linear", team_key="OPS"),
        ])
    runtime = _Runtime()
    reloads = []
    return cs, secrets_store, cfg, runtime, reloads


def _copy(cs, cfg, runtime, reloads, body):
    return _run(cs.copy_secrets(
        body, config=cfg, forge_runtime=runtime,
        reload=lambda: reloads.append(1)))


def test_repo_to_repo_copies_present_fields_and_skips_absent(
        tmp_path, monkeypatch):
    cs, store, cfg, runtime, reloads = _rig(tmp_path, monkeypatch)
    store.write_connection_secret("repo", "alpha", "token", "ghp_write")
    store.write_connection_secret("repo", "alpha", "token_ro", "ghp_read")
    # target's own reviewer_token must survive: absent-on-source is a skip,
    # never a delete
    store.write_connection_secret("repo", "beta", "reviewer_token", "ghp_rev")

    out = _copy(cs, cfg, runtime, reloads, {
        "source": {"scope": "repo", "name": "alpha"},
        "targets": [{"scope": "repo", "name": "beta"}]})

    assert out == {"ok": True, "source": {"scope": "repo", "name": "alpha"},
                   "results": [{"scope": "repo", "name": "beta",
                                "copied": ["token", "token_ro"],
                                "skipped": ["reviewer_token"]}]}
    assert store.read_connection_secret("repo", "beta", "token") == "ghp_write"
    assert store.read_connection_secret("repo", "beta", "token_ro") == "ghp_read"
    assert store.read_connection_secret(
        "repo", "beta", "reviewer_token") == "ghp_rev"
    assert runtime.cleared == ["beta"]     # breaker heals on the new token
    assert reloads == [1]                  # ONE rebuild for the batch


def test_repo_to_forge_issues_pmo_maps_write_token_to_api_key(
        tmp_path, monkeypatch):
    cs, store, cfg, runtime, reloads = _rig(tmp_path, monkeypatch)
    store.write_connection_secret("repo", "alpha", "token", "ghp_write")

    out = _copy(cs, cfg, runtime, reloads, {
        "source": {"scope": "repo", "name": "alpha"},
        "targets": [{"scope": "pmo", "name": "ghboard"}]})

    assert out["results"] == [{"scope": "pmo", "name": "ghboard",
                               "copied": ["api_key"], "skipped": []}]
    assert store.read_connection_secret(
        "pmo", "ghboard", "api_key") == "ghp_write"
    assert runtime.cleared == []           # pmo writes never touch breakers


def test_repo_to_linear_pmo_refused(tmp_path, monkeypatch):
    cs, store, cfg, runtime, reloads = _rig(tmp_path, monkeypatch)
    store.write_connection_secret("repo", "alpha", "token", "ghp_write")
    with pytest.raises(HTTPException) as e:
        _copy(cs, cfg, runtime, reloads, {
            "source": {"scope": "repo", "name": "alpha"},
            "targets": [{"scope": "pmo", "name": "linboard"}]})
    assert e.value.status_code == 422


def test_cross_forge_refuses_whole_batch_before_any_write(
        tmp_path, monkeypatch):
    cs, store, cfg, runtime, reloads = _rig(tmp_path, monkeypatch)
    store.write_connection_secret("repo", "alpha", "token", "ghp_write")
    with pytest.raises(HTTPException) as e:
        _copy(cs, cfg, runtime, reloads, {
            "source": {"scope": "repo", "name": "alpha"},
            "targets": [{"scope": "repo", "name": "beta"},
                        {"scope": "repo", "name": "gamma"}]})
    assert e.value.status_code == 422
    # beta was compatible but must NOT have been written (all-or-nothing)
    assert store.read_connection_secret("repo", "beta", "token") == ""
    assert reloads == []


def test_pmo_to_pmo_same_system_only(tmp_path, monkeypatch):
    cs, store, cfg, runtime, reloads = _rig(tmp_path, monkeypatch)
    store.write_connection_secret("pmo", "linboard", "api_key", "lin_api_x")

    out = _copy(cs, cfg, runtime, reloads, {
        "source": {"scope": "pmo", "name": "linboard"},
        "targets": [{"scope": "pmo", "name": "linboard2"}]})
    assert out["results"] == [{"scope": "pmo", "name": "linboard2",
                               "copied": ["api_key"], "skipped": []}]
    assert store.read_connection_secret(
        "pmo", "linboard2", "api_key") == "lin_api_x"

    for bad in ({"scope": "pmo", "name": "ghboard"},     # other system
                {"scope": "repo", "name": "alpha"}):     # pmo→repo
        with pytest.raises(HTTPException) as e:
            _copy(cs, cfg, runtime, reloads, {
                "source": {"scope": "pmo", "name": "linboard"},
                "targets": [bad]})
        assert e.value.status_code == 422


def test_unknown_cards_and_empty_shapes(tmp_path, monkeypatch):
    cs, store, cfg, runtime, reloads = _rig(tmp_path, monkeypatch)
    store.write_connection_secret("repo", "alpha", "token", "ghp_write")

    with pytest.raises(HTTPException) as e:
        _copy(cs, cfg, runtime, reloads, {
            "source": {"scope": "repo", "name": "ghost"},
            "targets": [{"scope": "repo", "name": "beta"}]})
    assert e.value.status_code == 404

    for body in (
            {"source": {"scope": "repo", "name": "alpha"}, "targets": []},
            {"source": {"scope": "repo", "name": "alpha"},
             "targets": [{"scope": "repo", "name": "ghost"}]},
            {"source": {"scope": "skill", "name": "alpha"},
             "targets": [{"scope": "repo", "name": "beta"}]},
            # source alone in targets = nothing to do
            {"source": {"scope": "repo", "name": "alpha"},
             "targets": [{"scope": "repo", "name": "alpha"}]}):
        with pytest.raises(HTTPException) as e:
            _copy(cs, cfg, runtime, reloads, body)
        assert e.value.status_code == 422
    assert store.read_connection_secret("repo", "beta", "token") == ""


def test_source_with_nothing_stored_refuses(tmp_path, monkeypatch):
    cs, store, cfg, runtime, reloads = _rig(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as e:
        _copy(cs, cfg, runtime, reloads, {
            "source": {"scope": "repo", "name": "alpha"},
            "targets": [{"scope": "repo", "name": "beta"}]})
    assert e.value.status_code == 422
    assert reloads == []
