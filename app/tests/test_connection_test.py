"""Connection-test HTTP contract: ok:False always carries `error`.

The SPA Test connection buttons print `tr.error`. Probe DTOs use `detail`.
Independent expected values are the planted detail strings.
"""

from __future__ import annotations

import asyncio

import pytest

from devcake.config import AppConfig, PMOInstance, RepoInstance
from devcake.ports.pmo import PMOHealth


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_forge_failed_probe_puts_detail_on_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.api import connections_service as cs

    repo = RepoInstance(
        name="devcakerepo", forge="github",
        url="https://github.com/example-org/devcake")
    secrets_store.write_connection_secret("repo", "devcakerepo", "token", "ghp_test")
    reason = (
        "repository access failed (HTTP 404); for a fine-grained PAT, "
        "select this repository and grant Contents and Pull requests read/write")

    class Runtime:
        def get(self, name):
            return object()

        async def refresh_health(self, name):
            return {
                "ok": False,
                "repository": "example-org/devcake",
                "can_push": False,
                "detail": reason,
            }

    out = _run(cs.test_forge(
        "devcakerepo", config=AppConfig(repos=[repo]), forge_runtime=Runtime()))
    assert out["ok"] is False
    assert out["error"] == reason


def test_pmo_failed_probe_puts_detail_on_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.api import connections_service as cs

    inst = PMOInstance(name="linear", system="linear", team_key="DEV")
    secrets_store.write_connection_secret("pmo", "linear", "api_key", "lin_test")
    reason = "team not found"

    class Adapter:
        async def health_probe(self, team_ref):
            return PMOHealth(ok=False, detail=reason)

        async def list_all(self, team_ref):
            return []

    class Mgr:
        pmo = Adapter()

    out = _run(cs.test_pmo(
        "linear", config=AppConfig(pmos=[inst]), managers={"linear": Mgr()}))
    assert out["ok"] is False
    assert out["error"] == reason


def test_skill_source_missing_returns_404(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from fastapi import HTTPException
    from devcake.api import connections_service as cs

    with pytest.raises(HTTPException) as e:
        _run(cs.test_skill_source(
            "ghost", config=AppConfig(pmos=[], repos=[]), repo_cache=None))
    assert e.value.status_code == 404


def test_skill_source_empty_url_ok_false(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import connections_service as cs
    from devcake.config import SkillSource

    src = SkillSource(name="shelf", url="")
    out = _run(cs.test_skill_source(
        "shelf",
        config=AppConfig(pmos=[], repos=[], skill_sources=[src]),
        repo_cache=None))
    assert out["ok"] is False
    assert "url" in out["error"].lower()


def test_skill_source_public_no_token_ok_true(tmp_path, monkeypatch):
    """A public skill source needs no token (the card's help says so) —
    the probe must run and succeed with nothing stored."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import connections_service as cs
    from devcake.config import SkillSource

    src = SkillSource(name="shelf", url="https://gh.example/o/shelf")

    class Cache:
        async def remote_head(self, name):
            return "feedc0de1234"

    out = _run(cs.test_skill_source(
        "shelf",
        config=AppConfig(pmos=[], repos=[], skill_sources=[src]),
        repo_cache=Cache()))
    assert out["ok"] is True
    assert out["remote_head"] == "feedc0de1234"


def test_skill_source_no_token_unreachable_hints_token(tmp_path, monkeypatch):
    """Unreachable with no token stored: still ok:False, and the error says
    a private repository would need a Read token."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import connections_service as cs
    from devcake.config import SkillSource

    src = SkillSource(name="shelf", url="https://gh.example/o/shelf")

    class Cache:
        async def remote_head(self, name):
            return None

    out = _run(cs.test_skill_source(
        "shelf",
        config=AppConfig(pmos=[], repos=[], skill_sources=[src]),
        repo_cache=Cache()))
    assert out["ok"] is False
    assert "no token" in out["error"]


def test_skill_source_probe_ok_true_with_remote_head(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.api import connections_service as cs
    from devcake.config import SkillSource

    src = SkillSource(name="shelf", forge="github",
                      url="https://github.com/example/skills")
    secrets_store.write_connection_secret(
        "skill", "shelf", "token_ro", "ghp_readonly")

    class Cache:
        async def remote_head(self, name):
            assert name == "shelf"
            return "abc123deadbeef"

    out = _run(cs.test_skill_source(
        "shelf",
        config=AppConfig(pmos=[], repos=[], skill_sources=[src]),
        repo_cache=Cache()))
    assert out["ok"] is True
    assert out["skill_source"] == "shelf"
    assert out["forge"] == "github"
    assert out["repo"] == "https://github.com/example/skills"
    assert out["remote_head"] == "abc123deadbeef"


def test_skill_source_unreachable_ok_false_never_500(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.api import connections_service as cs
    from devcake.config import SkillSource

    src = SkillSource(name="shelf", url="https://gh.example/o/shelf")
    secrets_store.write_connection_secret(
        "skill", "shelf", "token", "tok_fallback")

    class Cache:
        async def remote_head(self, name):
            return None

    out = _run(cs.test_skill_source(
        "shelf",
        config=AppConfig(pmos=[], repos=[], skill_sources=[src]),
        repo_cache=Cache()))
    assert out["ok"] is False
    assert out["error"]


def test_skill_source_probe_exception_ok_false(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.api import connections_service as cs
    from devcake.config import SkillSource

    src = SkillSource(name="shelf", url="https://gh.example/o/shelf")
    secrets_store.write_connection_secret(
        "skill", "shelf", "token_ro", "ghp_x")

    class Cache:
        async def remote_head(self, name):
            raise RuntimeError("git blew up with https://evil/secret")

    out = _run(cs.test_skill_source(
        "shelf",
        config=AppConfig(pmos=[], repos=[], skill_sources=[src]),
        repo_cache=Cache()))
    assert out["ok"] is False
    assert "error" in out
    assert "https://evil/secret" not in out["error"]


def test_skill_source_backed_probe_delegates_and_names_the_card(
        tmp_path, monkeypatch):
    """ADR-0039: a repo-backed source probes through its backing card — the
    unreachable error names that card (the fix lives on ITS Test button),
    never a token hint for a card that stores none."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import connections_service as cs
    from devcake.config import RepoInstance, SkillSource

    cfg = AppConfig(
        pmos=[],
        repos=[RepoInstance(name="work", url="https://github.com/o/r")],
        skill_sources=[SkillSource(name="shelf", backed_by="work")])

    class Down:
        async def remote_head(self, name):
            return None

    out = _run(cs.test_skill_source("shelf", config=cfg, repo_cache=Down()))
    assert out["ok"] is False
    assert "work" in out["error"] and "no token" not in out["error"]

    class Up:
        async def remote_head(self, name):
            return "feedc0de1234"

    out = _run(cs.test_skill_source("shelf", config=cfg, repo_cache=Up()))
    assert out["ok"] is True
    assert out["remote_head"] == "feedc0de1234"
    assert out["repo"] == "backed by work"
