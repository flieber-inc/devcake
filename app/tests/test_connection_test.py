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


# ── the connection test tells the truth about the branch ────────────────────

class _BranchCache:
    """RepoCache stand-in: what the mirror resolved, what the remote says,
    and whether a pinned ref exists on the remote."""

    def __init__(self, *, resolved="", remote="master", pin_exists=True,
                 remote_error=None):
        self._resolved, self._remote = resolved, remote
        self._pin_exists, self._remote_error = pin_exists, remote_error

    def resolved_branch(self, name):
        return self._resolved

    async def remote_default_branch(self, name):
        if self._remote_error:
            raise RuntimeError(self._remote_error)
        return self._remote

    async def remote_head(self, name):
        return "0" * 40 if self._pin_exists else None


class _OkRuntime:
    def __init__(self):
        self.forge = type("F", (), {})()

        async def get_pr_by_branch(branch):
            return None

        async def default_branch_protection(branch):
            self.probed = branch
            return None
        self.forge.get_pr_by_branch = get_pr_by_branch
        self.forge.default_branch_protection = default_branch_protection
        self.forge.reviewer_token = None

    def get(self, name):
        return self.forge

    async def refresh_health(self, name):
        return {"ok": True, "can_push": True, "detail": ""}


def _repo(tmp_path, monkeypatch, **kw):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    secrets_store.write_connection_secret("repo", "r", "token", "ghp_test")
    return RepoInstance(name="r", forge="github",
                        url="https://github.com/example-org/r", **kw)


def test_forge_test_reports_a_pin_the_repository_lacks(tmp_path, monkeypatch):
    from devcake.api import connections_service as cs
    repo = _repo(tmp_path, monkeypatch, default_branch="main")
    out = _run(cs.test_forge(
        "r", config=AppConfig(repos=[repo]), forge_runtime=_OkRuntime(),
        repo_cache=_BranchCache(remote="master", pin_exists=False)))
    assert out["ok"] is False
    assert "pins 'main'" in out["error"] and "'master'" in out["error"]
    assert out["pinned"] is True and out["pin_exists"] is False
    assert out["repository_default"] == "master"


def test_forge_test_blank_card_reports_the_repositorys_default(tmp_path, monkeypatch):
    from devcake.api import connections_service as cs
    repo = _repo(tmp_path, monkeypatch)
    rt = _OkRuntime()
    out = _run(cs.test_forge(
        "r", config=AppConfig(repos=[repo]), forge_runtime=rt,
        repo_cache=_BranchCache(resolved="", remote="master")))
    assert out["ok"] is True
    assert out["pinned"] is False
    assert out["default_branch"] == "master"        # the remote's answer
    assert out["repository_default"] == "master"
    assert rt.probed == "master"                     # protection asked on it
    # a resolved mirror wins over the remote's answer for the probe
    rt2 = _OkRuntime()
    out2 = _run(cs.test_forge(
        "r", config=AppConfig(repos=[repo]), forge_runtime=rt2,
        repo_cache=_BranchCache(resolved="trunk", remote="trunk")))
    assert out2["default_branch"] == "trunk" and rt2.probed == "trunk"
    # a remote probe failure is text, not a failed test
    out3 = _run(cs.test_forge(
        "r", config=AppConfig(repos=[repo]), forge_runtime=_OkRuntime(),
        repo_cache=_BranchCache(resolved="trunk", remote_error="timeout")))
    assert out3["ok"] is True and "timeout" in out3["repository_default_error"]


def test_discover_branch_single_and_bulk(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from devcake.api import connections_service as cs
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    blank = RepoInstance(name="blank", url="https://github.com/o/b")
    wrong = RepoInstance(name="wrong", url="https://github.com/o/w",
                         default_branch="main")
    good = RepoInstance(name="good", url="https://github.com/o/g",
                        default_branch="development")
    idle = RepoInstance(name="idle", url="")
    broken = RepoInstance(name="broken", url="https://github.com/o/x")

    class Cache:
        def resolved_branch(self, name):
            return ""

        async def remote_default_branch(self, name):
            if name == "broken":
                raise RuntimeError("fatal: Authentication failed")
            return "" if name == "empty" else "master"

        async def remote_head(self, name):
            return None if name == "wrong" else "0" * 40

    cfg = AppConfig(repos=[blank, wrong, good, idle, broken])
    one = _run(cs.discover_forge_branch("blank", config=cfg, repo_cache=Cache()))
    assert one == {"ok": True, "repo_name": "blank", "pinned": False,
                   "branch": "master"}
    pinned = _run(cs.discover_forge_branch("wrong", config=cfg, repo_cache=Cache()))
    assert pinned["branch"] == "master" and pinned["pin_exists"] is False
    with pytest.raises(HTTPException) as ex:
        _run(cs.discover_forge_branch("nope", config=cfg, repo_cache=Cache()))
    assert ex.value.status_code == 404
    assert _run(cs.discover_forge_branch("idle", config=cfg, repo_cache=Cache()))["ok"] is False
    bulk = _run(cs.discover_forge_branches(config=cfg, repo_cache=Cache()))
    assert bulk["ok"] is True
    r = bulk["results"]
    assert set(r) == {"blank", "wrong", "good", "broken"}      # idle skipped
    assert r["good"] == {"ok": True, "pinned": True, "branch": "master",
                         "pin_exists": True}
    assert r["broken"]["ok"] is False and "Authentication failed" in r["broken"]["error"]
    # an audit row per card, names only
    from devcake.settings_bundle import _audit_path
    rows = _audit_path().read_text() if _audit_path().exists() else ""
    assert "forge_branch_discovered" in rows and "master" in rows
