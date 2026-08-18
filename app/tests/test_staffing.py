"""Slice 2: refuse launch unless a matching ok receipt exists.

Public seam: require_staffed(dev_type, *, digest, store).
Independent expected values are the message literals and row names.
"""

from __future__ import annotations

import pytest

from devcake.config import DevType


class _Store:
    def __init__(self, rec):
        self.rec = rec

    def get(self, *, digest, template, cli_version):
        return self.rec


def _dt(template="grok-build"):
    return DevType(name="implementer", harness_template=template)


def test_sentinel_digest_names_the_wrapper_not_a_missing_receipt():
    from devcake.staffing import SENTINEL_DIGEST, HarnessNotStaffed, require_staffed

    with pytest.raises(HarnessNotStaffed, match="built without the bake wrapper") as exc:
        require_staffed(
            _dt(), digest=SENTINEL_DIGEST,
            store=_Store({"ok": True, "gated": True, "digest": SENTINEL_DIGEST}),
            baker_alive=True)
    assert "no receipt" not in str(exc.value).lower()


def test_missing_receipt_refuses():
    from devcake.staffing import HarnessNotStaffed, require_staffed

    with pytest.raises(HarnessNotStaffed, match="no receipt"):
        require_staffed(
            _dt(), digest="sha256:abc", store=_Store(None), baker_alive=True)


def test_ok_false_names_the_failing_required_row():
    from devcake.staffing import HarnessNotStaffed, require_staffed

    rec = {
        "ok": False,
        "gated": True,
        "digest": "sha256:abc",
        "rows": [
            {"name": "healthy", "required": True, "status": "pass"},
            {"name": "http_401", "required": True, "status": "fail"},
        ],
    }
    with pytest.raises(HarnessNotStaffed, match="http_401"):
        require_staffed(
            _dt(), digest="sha256:abc", store=_Store(rec), baker_alive=True)


def test_ok_false_lists_every_required_row_that_did_not_pass():
    from devcake.staffing import HarnessNotStaffed, require_staffed

    rec = {
        "ok": False,
        "gated": True,
        "digest": "sha256:abc",
        "rows": [
            {"name": "healthy", "required": True, "status": "fail"},
            {"name": "http_401", "required": True, "status": "pass"},
            {"name": "resume", "required": True, "status": "error",
             "detail": "first invocation exposed no session identity"},
        ],
    }
    with pytest.raises(HarnessNotStaffed) as caught:
        require_staffed(
            _dt(), digest="sha256:abc", store=_Store(rec), baker_alive=True)
    msg = str(caught.value)
    assert "healthy fail" in msg
    assert "resume error" in msg
    assert "first invocation exposed no session identity" in msg
    assert "http_401" not in msg
    assert caught.value.row == "healthy"


def test_required_skip_and_error_are_not_ok():
    from devcake.staffing import HarnessNotStaffed, require_staffed

    for status in ("skipped", "error"):
        rec = {
            "ok": False,
            "gated": True,
            "digest": "sha256:abc",
            "rows": [{"name": "empty", "required": True, "status": status}],
        }
        with pytest.raises(HarnessNotStaffed, match="empty"):
            require_staffed(
                _dt(), digest="sha256:abc", store=_Store(rec),
                baker_alive=True)

def test_dead_baker_refuses_even_with_an_ok_receipt():
    from devcake.staffing import HarnessNotStaffed, require_staffed

    with pytest.raises(HarnessNotStaffed, match="cannot vouch") as exc:
        require_staffed(
            _dt(), digest="sha256:abc",
            store=_Store({"ok": True, "gated": True, "digest": "sha256:abc"}),
            baker_alive=False)
    assert exc.value.kind == "baker"


def test_absent_baker_heartbeat_refuses_even_with_ok_receipt(monkeypatch):
    """Missing heartbeat is dead baker — not 'skip liveness and staff'."""
    import devcake.bake_status as bs
    from conftest import REAL_REQUIRE_STAFFED
    from devcake.staffing import HarnessNotStaffed

    monkeypatch.setattr(
        bs, "read_bake_status",
        lambda root=None: {"state": "idle", "jobs": [], "detail": ""},
    )

    with pytest.raises(HarnessNotStaffed, match="cannot vouch") as exc:
        # Unwrapped seam: baker_alive stays None so production liveness runs.
        REAL_REQUIRE_STAFFED(
            _dt(), digest="sha256:abc",
            store=_Store({"ok": True, "gated": True, "digest": "sha256:abc"}))
    assert exc.value.kind == "baker"


def test_ok_true_matching_digest_is_staffed():
    from devcake.staffing import require_staffed

    require_staffed(
        _dt(), digest="sha256:abc",
        store=_Store({"ok": True, "gated": True, "digest": "sha256:abc"}),
        baker_alive=True)


def test_receipt_without_gated_true_is_refused():
    """Absence of gated (or null) is fabricated — not fail-open."""
    from devcake.staffing import HarnessNotStaffed, require_staffed

    for rec in (
        {"ok": True, "digest": "sha256:abc"},
        {"ok": True, "digest": "sha256:abc", "gated": None},
    ):
        with pytest.raises(HarnessNotStaffed, match="not gated") as exc:
            require_staffed(
                _dt(), digest="sha256:abc", store=_Store(rec),
                baker_alive=True)
        assert exc.value.kind == "fabricated"

def test_oauth_does_not_launch_when_not_staffed(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from devcake.adapters.files.receipts import FileReceiptStore
    from devcake.domain.oauth import OAuthManager
    from devcake.domain.runs import RunManager
    from devcake.staffing import HarnessNotStaffed
    from test_oauth import FakeExecutor, NullMessaging

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEVCAKE_APP_DIGEST", "sha256:abc")
    rec_dir = tmp_path / "harness_receipts"
    rec_dir.mkdir()
    (rec_dir / "grok-build@0.2.112.json").write_text(json.dumps({
        "digest": "sha256:abc", "template": "grok-build",
        "cli_version": "0.2.112", "ok": False, "gated": True,
        "rows": [{"name": "http_401", "required": True, "status": "fail"}],
    }))
    executor = FakeExecutor()
    from devcake.adapters.files.run_store import RunStore
    runs = RunManager(RunStore(tmp_path / "runs"), NullMessaging(), executor)
    mgr = OAuthManager(
        runs, NullMessaging(),
        {"main-dev": _dt()},
        receipt_store=FileReceiptStore(rec_dir))
    import asyncio
    with pytest.raises(HarnessNotStaffed, match="http_401"):
        asyncio.new_event_loop().run_until_complete(mgr._start_inner("main-dev"))
    assert executor.params is None


def test_steward_does_not_launch_when_not_staffed(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timezone

    from devcake.adapters.files.receipts import FileReceiptStore
    from devcake.adapters.github import GitHubForge
    from devcake.config import AppConfig
    from devcake.domain.model import Mission
    from fakes import make_mission_manager

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    rec_dir = tmp_path / "harness_receipts"
    rec_dir.mkdir()
    (rec_dir / "grok-build@0.2.112.json").write_text(json.dumps({
        "digest": "sha256:test", "ok": False, "gated": True,
        "rows": [{"name": "empty", "required": True, "status": "error"}],
    }))
    from test_oauth import FakeExecutor, NullMessaging
    from devcake.adapters.files.run_store import RunStore
    from devcake.domain.runs import RunManager
    executor = FakeExecutor()
    runs = RunManager(RunStore(tmp_path / "runs"), NullMessaging(), executor)
    mgr = make_mission_manager(
        runs=runs, messaging=NullMessaging(),
        forge=GitHubForge("https://github.com/o/r", "tok"),
        config=AppConfig())
    mgr.receipt_store = FileReceiptStore(rec_dir)
    dt = _dt()
    m = Mission(pmo_id="p1", pmo_kind="issue", key="T-1", title="t",
                status="backlog", labels={"DEVCAKE"},
                updated_at=datetime.now(timezone.utc))
    import asyncio
    run = asyncio.new_event_loop().run_until_complete(
        mgr.dispatch_steward(dt, [m]))
    assert run is None
    assert executor.params is None


def test_hello_launch_path_does_not_import_staffing():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "devcake" / "domain"
            / "runs.py").read_text()
    assert "require_staffed" not in text
    assert "HELLO_IMAGE" in text


def test_require_staffed_sits_next_to_the_secret_env_gate():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "devcake" / "domain"
            / "orchestrator" / "dispatch.py").read_text()
    staff = text.index("require_staffed(")
    skill = text.index("_skill_payload")
    secret = text.index("missing_referenced_secret_env")
    assert secret < staff < skill


def test_fabricated_ungated_receipt_is_refused():
    from devcake.staffing import HarnessNotStaffed, require_staffed

    rec = {"ok": True, "digest": "sha256:abc", "gated": False, "rows": []}
    with pytest.raises(HarnessNotStaffed, match="not gated"):
        require_staffed(
            _dt(), digest="sha256:abc", store=_Store(rec), baker_alive=True)


def test_every_registry_template_is_gated_the_same_way():
    """Bake verb is compile+probe+receipt for every HARNESSES id. No skip."""
    from devcake.harness import HARNESSES
    from devcake.staffing import HarnessNotStaffed, require_staffed

    for template in HARNESSES:
        with pytest.raises(HarnessNotStaffed, match="no receipt"):
            require_staffed(
                _dt(template), digest="sha256:abc", store=_Store(None),
                baker_alive=True)

def test_pin_summary_has_no_host_command():
    """The host baker watches the keep-set. SPA must not assign terminal homework."""
    from devcake.staffing import receipt_summary

    summary = receipt_summary(
        {"implementer": _dt()}, digest="sha256:abc", store=_Store(None))
    entry = summary["dev_types"]["implementer"]
    assert "command" not in entry
    assert entry["ok"] is False
    assert entry["state"] == "waiting"


def test_pin_summary_baking_state_comes_from_the_host_status():
    from devcake.staffing import receipt_summary

    bake_status = {
        "state": "baking",
        "jobs": [{"template": "grok-build", "cli_version": "0.2.112",
                  "state": "baking"}],
    }
    summary = receipt_summary(
        {"implementer": _dt()}, digest="sha256:abc", store=_Store(None),
        bake_status=bake_status)
    assert summary["dev_types"]["implementer"]["state"] == "baking"
    assert summary["dev_types"]["implementer"]["ok"] is False
