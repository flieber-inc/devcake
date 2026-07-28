"""Hygiene slices: mission audit redact, OAuth log scrub, probe client errors,
credential upload caps, atomic credential write."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from devcake.security import MASK, register_runtime_secret, unregister_runtime_secret


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def test_mission_audit_detail_redacted_on_disk(tmp_path, monkeypatch):
    import devcake.domain.orchestrator.markers as markers
    from devcake.domain.orchestrator import feed

    audit = tmp_path / "events.jsonl"
    monkeypatch.setattr(markers, "AUDIT_PATH", audit)
    secret = "hygiene-audit-secret-token-xyz"
    register_runtime_secret("test:hygiene-audit", secret)
    try:
        mgr = SimpleNamespace(instance_name="lin", _grace_next=set())
        feed._audit(mgr, "ISSUE-1", "activity_repo_push_failed",
                    f"push failed: {secret} mid")
        line = json.loads(audit.read_text().strip().splitlines()[-1])
        assert secret not in line["detail"]
        assert MASK in line["detail"] or "REDACTED" in line["detail"]
        assert "ISSUE-1" == line["pmo_id"]
        assert "ISSUE-1" in mgr._grace_next
    finally:
        unregister_runtime_secret("test:hygiene-audit")


def test_oauth_run_log_does_not_log_url_or_code(tmp_path, caplog):
    from devcake.adapters.files.run_store import RunStore
    from devcake.domain.run import Run
    from devcake.domain.runs import RunManager

    rid = "r-oauth-1"
    store = RunStore(root=tmp_path / "runs")
    mgr = RunManager(store, messaging=None, executor=None)
    store.save(Run(run_id=rid, mission_key="OAUTH", mission_type="OAUTH",
                   dev_type="main-dev", seq=1))
    seen = []
    mgr.oauth_mgr = type("Stub", (), {
        "on_log": lambda self, r, p: seen.append((r, p)),
        "sessions": {}})()
    url = "https://accounts.x.ai/device?user_code=ABCD-EFGH"
    code = "ABCD-EFGH"
    with caplog.at_level(logging.INFO, logger="devcake.runs"):
        run_coro(mgr.handle(rid, "run.log", {"oauth_url": url, "code": code}))
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert url not in joined
    assert code not in joined
    assert "oauth progress" in joined
    assert seen == [(rid, {"oauth_url": url, "code": code})]


def test_probe_client_error_hides_token_shaped_unknown():
    from devcake.api.connections_service import _probe_client_error
    from devcake.ports.pmo import PMOTransient

    secretish = "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz012345"
    msg = _probe_client_error(RuntimeError(secretish))
    assert "ghp_" not in msg
    assert "Bearer" not in msg
    assert "see app logs" in msg

    transient = _probe_client_error(PMOTransient("rate limited by vendor"))
    assert "rate limited" in transient


def test_write_credential_file_atomic_and_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as s

    p = s.write_credential_file("coder", "auth.json", '{"tok":"x"}')
    assert p.read_text() == '{"tok":"x"}'
    assert (p.stat().st_mode & 0o777) == 0o600

    with pytest.raises(ValueError, match="too large"):
        s.write_credential_file(
            "coder", "big.json", "x" * (s.MAX_CREDENTIAL_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="invalid credential"):
        s.write_credential_file("coder", "../etc/passwd", "nope")
    with pytest.raises(ValueError, match="invalid credential"):
        s.write_credential_file("harness", "x.json", "nope")


def test_upload_credentials_rejects_oversize_and_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import devtypes_service as dts
    from devcake.config import DevType
    from fastapi import HTTPException

    dts_types = {"coder": DevType(name="coder", harness_template="grok-build")}
    breakers = {"coder": "DEV_AUTH"}

    with pytest.raises(HTTPException) as e:
        run_coro(dts.upload_credentials(
            "coder",
            {"filename": "../secrets/x", "content": "x"},
            dev_types=dts_types, shared_breakers=breakers))
    assert e.value.status_code == 422

    from devcake import secrets as s
    with pytest.raises(HTTPException) as e2:
        run_coro(dts.upload_credentials(
            "coder",
            {"filename": "ok.json",
             "content": "y" * (s.MAX_CREDENTIAL_FILE_BYTES + 1)},
            dev_types=dts_types, shared_breakers=breakers))
    assert e2.value.status_code == 422

    out = run_coro(dts.upload_credentials(
        "coder",
        {"filename": "ok.json", "content": "credential-body"},
        dev_types=dts_types, shared_breakers=breakers))
    assert out["stored"] == "coder/ok.json"
    assert "coder" not in breakers
    path = tmp_path / "secrets" / "coder" / "ok.json"
    assert path.read_text() == "credential-body"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_delete_connection_instance_keeps_redaction(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as s
    from devcake.security import redact

    s.write_connection_secret("pmo", "lin", "api_key", "revoked-token-value1")
    s.delete_connection_instance("pmo", "lin")
    assert not (tmp_path / "secrets" / "connections" / "pmo-lin.json").exists()
    assert "revoked-token-value1" not in redact("leak revoked-token-value1 end")


def test_register_all_reloads_credential_files(tmp_path, monkeypatch):
    """Boot register_all must re-register raw credential files (not JSON-only)."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as s
    from devcake.security import redact, unregister_runtime_secret

    secret = "raw-oauth-blob-not-json!!"
    s.write_credential_file("coder", "auth.txt", secret)
    # Simulate process restart: drop the runtime registration only
    unregister_runtime_secret("cred:coder:auth.txt")
    assert secret in redact(f"leak {secret} end")  # not registered
    s.register_all()
    try:
        assert secret not in redact(f"leak {secret} end")
    finally:
        unregister_runtime_secret("cred:coder:auth.txt")
