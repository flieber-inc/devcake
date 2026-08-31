"""CAKE-182 — operator apply-protection admin API (single + bulk).

Public seam: connections_service.apply_forge_protection /
apply_forge_protection_bulk (and the matching POST routes). Fakes at the
forge port; assertions use literal outcome / error shapes.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from devcake.config import AppConfig, RepoInstance
from devcake.ports.forge import (
    ApplyProtectionResult,
    BranchProtection,
    ForgeCapabilities,
    ForgeError,
    ProtectionShape,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeForge:
    """Port-shaped forge: apply + thin protection probe."""

    def __init__(
            self, *,
            apply_result: ApplyProtectionResult | None = None,
            apply_exc: Exception | None = None,
            protection: BranchProtection | None = None,
            caps: ForgeCapabilities | None = None):
        self._apply_result = apply_result
        self._apply_exc = apply_exc
        self._protection = protection
        self.capabilities = caps or ForgeCapabilities()
        self.apply_calls: list[str] = []

    async def apply_default_branch_protection(self, branch: str = "main"):
        self.apply_calls.append(branch)
        if self._apply_exc is not None:
            raise self._apply_exc
        assert self._apply_result is not None
        return self._apply_result

    async def default_branch_protection(self, branch: str = "main"):
        return self._protection


class _MultiRuntime:
    """name → forge / instance for bulk membership tests."""

    def __init__(self, mapping: dict[str, tuple[_FakeForge, RepoInstance]]):
        self._mapping = mapping

    def get(self, name: str):
        pair = self._mapping.get(name)
        return pair[0] if pair else None

    def instance(self, name: str):
        pair = self._mapping.get(name)
        return pair[1] if pair else None

    @property
    def forges(self):
        return {n: f for n, (f, _) in self._mapping.items()}


def _shape(**kwargs) -> ProtectionShape:
    return ProtectionShape(**kwargs)


# ── single ───────────────────────────────────────────────────────────────────


def test_apply_single_applied_ok_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.api import connections_service as cs
    from devcake.api import health as health_mod
    from devcake import settings_bundle as sb

    repo = RepoInstance(
        name="work", forge="github",
        url="https://github.com/example-org/work")
    secrets_store.write_connection_secret("repo", "work", "token", "ghp_write")
    shape = _shape(required_status_checks=["ci"])
    forge = _FakeForge(
        apply_result=ApplyProtectionResult(outcome="applied", shape=shape),
        protection=BranchProtection(protected=False))
    rt = _MultiRuntime({"work": (forge, repo)})

    health_mod._protection_cache["ts"] = 999.0
    out = _run(cs.apply_forge_protection(
        "work", config=AppConfig(repos=[repo]), forge_runtime=rt))

    assert out["ok"] is True
    assert out["repo"] == "work"
    assert out["outcome"] == "applied"
    assert out["shape"]["required_status_checks"] == ["ci"]
    assert forge.apply_calls == ["main"]
    assert health_mod._protection_cache["ts"] == 0.0

    line = (tmp_path / "state" / "events.jsonl").read_text().strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["action"] == "forge_protection_applied"
    assert "repo=work" in rec["detail"]
    assert "outcome=applied" in rec["detail"]
    assert "ghp_write" not in rec["detail"]


def test_apply_single_already_as_strict(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.api import connections_service as cs

    repo = RepoInstance(
        name="work", forge="gitea",
        url="https://gitea.example/o/work")
    secrets_store.write_connection_secret("repo", "work", "token", "tok")
    shape = _shape(require_status_checks_unscoped=True)
    forge = _FakeForge(
        apply_result=ApplyProtectionResult(
            outcome="already_as_strict", shape=shape))
    rt = _MultiRuntime({"work": (forge, repo)})

    out = _run(cs.apply_forge_protection(
        "work", config=AppConfig(repos=[repo]), forge_runtime=rt))

    assert out["ok"] is True
    assert out["outcome"] == "already_as_strict"
    assert out["shape"]["require_status_checks_unscoped"] is True

    line = (tmp_path / "state" / "events.jsonl").read_text().strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["action"] == "forge_protection_applied"
    assert "outcome=already_as_strict" in rec["detail"]


def test_apply_single_403_ok_false_preserves_message(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.api import connections_service as cs

    repo = RepoInstance(
        name="work", forge="github",
        url="https://github.com/example-org/work")
    secrets_store.write_connection_secret("repo", "work", "token", "ghp_x")
    msg = (
        "github write token lacks permission to set branch protection "
        "(needs administration): Resource not accessible by personal access token"
    )
    forge = _FakeForge(apply_exc=ForgeError(msg, status=403))
    rt = _MultiRuntime({"work": (forge, repo)})

    out = _run(cs.apply_forge_protection(
        "work", config=AppConfig(repos=[repo]), forge_runtime=rt))

    assert out["ok"] is False
    assert out["repo"] == "work"
    assert out["status"] == 403
    assert out["error"] == msg

    line = (tmp_path / "state" / "events.jsonl").read_text().strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["action"] == "forge_protection_applied"
    assert "repo=work" in rec["detail"]
    assert "error=" in rec["detail"]
    assert "ghp_x" not in rec["detail"]


def test_apply_single_unknown_repo_404(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import connections_service as cs

    with pytest.raises(HTTPException) as e:
        _run(cs.apply_forge_protection(
            "ghost", config=AppConfig(repos=[]),
            forge_runtime=_MultiRuntime({})))
    assert e.value.status_code == 404


def test_apply_single_reference_only_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.api import connections_service as cs

    repo = RepoInstance(
        name="ref", forge="github",
        url="https://github.com/example-org/docs")
    secrets_store.write_connection_secret("repo", "ref", "token_ro", "ghp_ro")
    forge = _FakeForge(
        apply_result=ApplyProtectionResult(
            outcome="applied", shape=_shape()))
    rt = _MultiRuntime({"ref": (forge, repo)})

    out = _run(cs.apply_forge_protection(
        "ref", config=AppConfig(repos=[repo]), forge_runtime=rt))

    assert out["ok"] is False
    assert out["repo"] == "ref"
    assert "reference" in out["error"].lower()
    assert forge.apply_calls == []


# ── bulk ─────────────────────────────────────────────────────────────────────


def test_bulk_skips_reference_only_includes_unprotected(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.api import connections_service as cs

    work = RepoInstance(
        name="work", forge="github",
        url="https://github.com/example-org/work")
    ref = RepoInstance(
        name="docs", forge="github",
        url="https://github.com/example-org/docs")
    protected = RepoInstance(
        name="safe", forge="github",
        url="https://github.com/example-org/safe")
    secrets_store.write_connection_secret("repo", "work", "token", "tok_w")
    secrets_store.write_connection_secret("repo", "docs", "token_ro", "tok_ro")
    secrets_store.write_connection_secret("repo", "safe", "token", "tok_s")

    work_f = _FakeForge(
        apply_result=ApplyProtectionResult(
            outcome="applied", shape=_shape()),
        protection=BranchProtection(protected=False))
    ref_f = _FakeForge(
        apply_result=ApplyProtectionResult(
            outcome="applied", shape=_shape()),
        protection=BranchProtection(protected=False))
    safe_f = _FakeForge(
        apply_result=ApplyProtectionResult(
            outcome="already_as_strict", shape=_shape()),
        protection=BranchProtection(protected=True))
    rt = _MultiRuntime({
        "work": (work_f, work),
        "docs": (ref_f, ref),
        "safe": (safe_f, protected),
    })

    out = _run(cs.apply_forge_protection_bulk(
        config=AppConfig(repos=[work, ref, protected]), forge_runtime=rt))

    assert out["ok"] is True
    names = [r["repo"] for r in out["results"]]
    assert names == ["work"]
    assert out["results"][0]["outcome"] == "applied"
    assert work_f.apply_calls == ["main"]
    assert ref_f.apply_calls == []
    assert safe_f.apply_calls == []


def test_bulk_403_does_not_stop_siblings(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.api import connections_service as cs

    a = RepoInstance(
        name="a", forge="github", url="https://github.com/o/a")
    b = RepoInstance(
        name="b", forge="github", url="https://github.com/o/b")
    secrets_store.write_connection_secret("repo", "a", "token", "tok_a")
    secrets_store.write_connection_secret("repo", "b", "token", "tok_b")

    forbidden = (
        "github write token lacks permission to set branch protection "
        "(needs administration)"
    )
    a_f = _FakeForge(
        apply_exc=ForgeError(forbidden, status=403),
        protection=BranchProtection(protected=False))
    b_f = _FakeForge(
        apply_result=ApplyProtectionResult(
            outcome="applied", shape=_shape()),
        protection=None)  # unknown / None → treat as unprotected
    rt = _MultiRuntime({"a": (a_f, a), "b": (b_f, b)})

    out = _run(cs.apply_forge_protection_bulk(
        config=AppConfig(repos=[a, b]), forge_runtime=rt))

    assert out["ok"] is True
    assert len(out["results"]) == 2
    by_name = {r["repo"]: r for r in out["results"]}
    assert by_name["a"]["ok"] is False
    assert by_name["a"]["status"] == 403
    assert by_name["a"]["error"] == forbidden
    assert by_name["b"]["ok"] is True
    assert by_name["b"]["outcome"] == "applied"

    lines = (tmp_path / "state" / "events.jsonl").read_text().strip().splitlines()
    actions = [json.loads(ln) for ln in lines
               if json.loads(ln)["action"] == "forge_protection_applied"]
    assert len(actions) == 2
    details = " ".join(r["detail"] for r in actions)
    assert "repo=a" in details and "repo=b" in details


def test_bulk_empty_when_all_protected_or_ineligible(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.api import connections_service as cs

    safe = RepoInstance(
        name="safe", forge="github", url="https://github.com/o/safe")
    secrets_store.write_connection_secret("repo", "safe", "token", "tok")
    forge = _FakeForge(
        protection=BranchProtection(protected=True, requires_reviews=True))
    rt = _MultiRuntime({"safe": (forge, safe)})

    out = _run(cs.apply_forge_protection_bulk(
        config=AppConfig(repos=[safe]), forge_runtime=rt))

    assert out["ok"] is True
    assert out["results"] == []
    assert forge.apply_calls == []
