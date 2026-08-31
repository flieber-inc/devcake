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
            # unhashable scope must 422, never a TypeError 500
            {"source": {"scope": ["repo"], "name": "alpha"},
             "targets": [{"scope": "repo", "name": "beta"}]},
            # the source among the targets is a caller bug — strict 422
            {"source": {"scope": "repo", "name": "alpha"},
             "targets": [{"scope": "repo", "name": "alpha"}]},
            # so is a duplicate target (all-or-nothing doctrine)
            {"source": {"scope": "repo", "name": "alpha"},
             "targets": [{"scope": "repo", "name": "beta"},
                         {"scope": "repo", "name": "beta"}]}):
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


def test_same_forge_different_host_refused(tmp_path, monkeypatch):
    """Same forge id is NOT enough — gitea/gitlab self-host and GitHub has
    Enterprise, so a PAT is only valid per host."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as store
    from devcake.api import connections_service as cs

    cfg = AppConfig(repos=[
        RepoInstance(name="inhouse", forge="gitea",
                     url="http://gitea:3000/org/inhouse"),
        RepoInstance(name="external", forge="gitea",
                     url="https://gitea.example.com/org/external"),
    ], pmos=[])
    store.write_connection_secret("repo", "inhouse", "token", "tok-a")
    with pytest.raises(HTTPException) as e:
        _run(cs.copy_secrets(
            {"source": {"scope": "repo", "name": "inhouse"},
             "targets": [{"scope": "repo", "name": "external"}]},
            config=cfg, forge_runtime=_Runtime(), reload=lambda: None))
    assert e.value.status_code == 422
    assert "host" in str(e.value.detail)
    assert store.read_connection_secret("repo", "external", "token") == ""


def test_issues_board_host_must_match_the_repo_host(tmp_path, monkeypatch):
    """A github_issues board on GHE must not take a github.com repo's PAT;
    api.github.com counts as github.com via the registry alias groups."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as store
    from devcake.api import connections_service as cs

    cfg = AppConfig(
        repos=[RepoInstance(name="alpha", forge="github",
                            url="https://github.com/example-org/alpha")],
        pmos=[PMOInstance(name="ghe", system="github_issues",
                          team_key="org/board",
                          api_base="https://ghe.corp/api/v3"),
              PMOInstance(name="dotcom", system="github_issues",
                          team_key="org/board2",
                          api_base="https://api.github.com")])
    store.write_connection_secret("repo", "alpha", "token", "ghp_x")
    with pytest.raises(HTTPException) as e:
        _run(cs.copy_secrets(
            {"source": {"scope": "repo", "name": "alpha"},
             "targets": [{"scope": "pmo", "name": "ghe"}]},
            config=cfg, forge_runtime=_Runtime(), reload=lambda: None))
    assert e.value.status_code == 422 and "host" in str(e.value.detail)
    out = _run(cs.copy_secrets(
        {"source": {"scope": "repo", "name": "alpha"},
         "targets": [{"scope": "pmo", "name": "dotcom"}]},
        config=cfg, forge_runtime=_Runtime(), reload=lambda: None))
    assert out["results"][0]["copied"] == ["api_key"]


def test_dry_run_reports_rows_and_writes_nothing(tmp_path, monkeypatch):
    cs, store, cfg, runtime, reloads = _rig(tmp_path, monkeypatch)
    store.write_connection_secret("repo", "alpha", "token", "ghp_write")

    out = _run(cs.copy_secrets(
        {"dry_run": True,
         "source": {"scope": "repo", "name": "alpha"},
         "targets": [{"scope": "repo", "name": "beta"},
                     {"scope": "repo", "name": "gamma"},
                     {"scope": "pmo", "name": "ghboard"},
                     {"scope": "repo", "name": "ghost"}]},
        config=cfg, forge_runtime=runtime, reload=lambda: reloads.append(1)))
    rows = {(r["scope"], r["name"]): r for r in out["targets"]}
    assert out["dry_run"] is True
    assert rows[("repo", "beta")]["eligible"] is True
    assert rows[("repo", "beta")]["receives"] == ["token"]
    assert rows[("repo", "beta")]["skipped"] == ["reviewer_token", "token_ro"]
    assert rows[("pmo", "ghboard")]["receives"] == ["api_key"]
    assert rows[("repo", "gamma")]["eligible"] is False   # cross-forge
    assert rows[("repo", "ghost")]["eligible"] is False   # unknown card
    assert "reason" in rows[("repo", "ghost")]
    # nothing written, no adapter rebuild
    assert store.read_connection_secret("repo", "beta", "token") == ""
    assert reloads == [] and runtime.cleared == []


def test_mid_batch_write_failure_rebuilds_adapters_and_names_written(
        tmp_path, monkeypatch):
    """The 'no partial copy' promise is validation-side; a mid-batch DISK
    failure must still rebuild adapters to match what reached the store
    and say which targets were written."""
    cs, store, cfg, runtime, reloads = _rig(tmp_path, monkeypatch)
    store.write_connection_secret("repo", "alpha", "token", "ghp_write")
    real = store.write_connection_fields
    calls = []

    def flaky(scope, name, fields):
        calls.append(name)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(scope, name, fields)

    monkeypatch.setattr(store, "write_connection_fields", flaky)
    with pytest.raises(HTTPException) as e:
        _run(cs.copy_secrets(
            {"source": {"scope": "repo", "name": "alpha"},
             "targets": [{"scope": "repo", "name": "beta"},
                         {"scope": "pmo", "name": "ghboard"}]},
            config=cfg, forge_runtime=runtime,
            reload=lambda: reloads.append(1)))
    assert e.value.status_code == 500
    assert "repo:beta" in str(e.value.detail)
    assert reloads == [1]                       # adapters match the disk
    assert store.read_connection_secret("repo", "beta", "token") == "ghp_write"


def test_copy_writes_one_audit_event_with_names_only(tmp_path, monkeypatch):
    cs, store, cfg, runtime, reloads = _rig(tmp_path, monkeypatch)
    store.write_connection_secret("repo", "alpha", "token", "ghp_secret_value")
    events = []
    import devcake.settings_bundle as sb
    monkeypatch.setattr(sb, "audit_event",
                        lambda action, detail="": events.append(
                            (action, detail)))
    _copy(cs, cfg, runtime, reloads, {
        "source": {"scope": "repo", "name": "alpha"},
        "targets": [{"scope": "repo", "name": "beta"}]})
    assert len(events) == 1
    action, detail = events[0]
    assert action == "secrets_copied"
    assert "repo:alpha" in detail and "repo:beta" in detail
    assert "ghp_secret_value" not in detail


def test_write_connection_fields_is_one_file_update(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as store
    store.write_connection_secret("repo", "beta", "reviewer_token", "keep")
    store.write_connection_fields("repo", "beta",
                                  {"token": "rw", "token_ro": "ro"})
    assert store.read_connection_secret("repo", "beta", "token") == "rw"
    assert store.read_connection_secret("repo", "beta", "token_ro") == "ro"
    assert store.read_connection_secret("repo", "beta",
                                        "reviewer_token") == "keep"
