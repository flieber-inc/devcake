"""Slice 1: resolve_image is the launch chokepoint. Empty pin = today's image.

No cli_version field. Receipts are readable and do not change launch.
"""

from __future__ import annotations

import json
from pathlib import Path

from devcake.config import DevType


_LAUNCH = [
    Path(__file__).resolve().parents[1] / "devcake" / "domain" / "orchestrator" / "dispatch.py",
    Path(__file__).resolve().parents[1] / "devcake" / "domain" / "orchestrator" / "steward.py",
    Path(__file__).resolve().parents[1] / "devcake" / "domain" / "oauth.py",
]
_RUNS = Path(__file__).resolve().parents[1] / "devcake" / "domain" / "runs.py"


def test_launch_sites_call_resolve_image_not_harness_image():
    """The three harness launch sites share one function. Hello is not one."""
    for path in _LAUNCH:
        text = path.read_text()
        assert "resolve_image(" in text, path.name
        assert "image=HARNESSES[" not in text, path.name
        assert "image=harness.image" not in text, path.name


def test_hello_launch_stays_on_HELLO_IMAGE():
    text = _RUNS.read_text()
    assert "HELLO_IMAGE" in text
    assert "resolve_image" not in text


def test_resolve_image_empty_pin_is_todays_house_image():
    """Independent expected: the literal house tag string, not HARNESSES[t].image."""
    from devcake.harness import resolve_image

    dt = DevType(name="implementer", harness_template="grok-build")
    assert resolve_image(dt) == "devcake/dev-grok-build:latest"
    dt = DevType(name="judgment", harness_template="claude-code")
    assert resolve_image(dt) == "devcake/dev-claude-code:latest"
    dt = DevType(name="coder", harness_template="codex")
    assert resolve_image(dt) == "devcake/dev-codex:latest"


def test_receipt_store_reads_row_level_receipt(tmp_path):
    from devcake.adapters.files.receipts import FileReceiptStore

    planted = {
        "digest": "sha256:test",
        "template": "grok-build",
        "cli_version": "0.2.112",
        "rows": [{"name": "http_401", "required": True, "status": "pass"}],
        "ok": True,
    }
    dest = tmp_path / "grok-build@0.2.112.json"
    dest.write_text(json.dumps(planted))
    store = FileReceiptStore(tmp_path)
    rec = store.get(digest="sha256:test", template="grok-build",
                    cli_version="0.2.112")
    assert rec is not None
    assert rec["ok"] is True
    assert rec["rows"][0]["name"] == "http_401"
    assert rec["rows"][0]["status"] == "pass"
    assert store.get(digest="sha256:test", template="grok-build",
                     cli_version="9.9.9") is None
    assert store.get(digest="sha256:other", template="grok-build",
                     cli_version="0.2.112") is None


def test_failing_receipt_does_not_change_resolve_image(tmp_path):
    """Fail-open: a planted ok:false receipt must not change the image string."""
    from devcake.adapters.files.receipts import FileReceiptStore
    from devcake.harness import resolve_image

    dest = tmp_path / "grok-build@0.2.112.json"
    dest.write_text(json.dumps({
        "digest": "sha256:test", "template": "grok-build",
        "cli_version": "0.2.112", "rows": [], "ok": False,
    }))
    store = FileReceiptStore(tmp_path)
    assert store.get(digest="sha256:test", template="grok-build",
                     cli_version="0.2.112")["ok"] is False
    dt = DevType(name="implementer", harness_template="grok-build")
    assert resolve_image(dt) == "devcake/dev-grok-build:latest"


def test_publish_keep_set_is_the_template_list_not_yaml(tmp_path):
    from devcake.keep_set import publish_keep_set

    publish_keep_set(
        {
            "implementer": DevType(name="implementer",
                                   harness_template="grok-build"),
            "judgment": DevType(name="judgment",
                                harness_template="claude-code"),
            "also-grok": DevType(name="also-grok",
                                 harness_template="grok-build"),
        },
        root=tmp_path,
    )
    body = json.loads((tmp_path / "harness_keep_set.json").read_text())
    assert body == {"templates": ["claude-code", "grok-build"]}


def test_upsert_and_delete_publish_the_keep_set(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import devtypes_service
    from devcake import config as config_mod
    from devcake.config import AppConfig, Assignment

    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")
    (tmp_path / "config" / "dev_types").mkdir(parents=True)

    loop = asyncio.new_event_loop()
    dts: dict = {}
    loop.run_until_complete(devtypes_service.upsert_dev_type(
        {"name": "implementer", "harness_template": "grok-build"},
        dev_types=dts))
    body = json.loads((tmp_path / "harness_keep_set.json").read_text())
    assert body["templates"] == ["grok-build"]
    cfg = AppConfig(assignments={
        mt: Assignment(dev_type="judgment")
        for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")})
    loop.run_until_complete(devtypes_service.remove_dev_type(
        "implementer", config=cfg, dev_types=dts))
    body = json.loads((tmp_path / "harness_keep_set.json").read_text())
    assert body["templates"] == []
    loop.close()
