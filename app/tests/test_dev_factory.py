"""Host factory: keep-set → bake list. App never Docker; host validates.

Public seam: load_keep_set(path) → KeepSet | None;
plan_bakes(keep_set, *, digest, receipts) → tuple[BakeJob, ...];
image_ref(template, cli_version, *, tag, house) → str.

Independent expected values are the planted JSON pins and the literal
image strings. The factory must refuse an invalid keep-set rather than
trust the app file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_FACTORY_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "scripts",
    Path("/srv/repo-scripts"),
]


def _load_factory():
    root = next((p for p in _FACTORY_CANDIDATES if p.is_dir()), None)
    assert root is not None, "scripts/ missing — bind scripts → /srv/repo-scripts"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import dev_factory
    return dev_factory


def test_absent_keep_set_is_virgin():
    factory = _load_factory()
    assert factory.load_keep_set(Path("/no/such/harness_keep_set.json")) is None


def test_valid_keep_set_lists_unique_pins(tmp_path):
    factory = _load_factory()
    dest = tmp_path / "harness_keep_set.json"
    dest.write_text(json.dumps({
        "templates": ["claude-code", "grok-build"],
        "pins": [
            {"template": "claude-code", "cli_version": "2.1.229"},
            {"template": "grok-build", "cli_version": "0.2.112"},
            {"template": "grok-build", "cli_version": "1.0.4"},
        ],
    }))
    ks = factory.load_keep_set(dest)
    assert [(p.template, p.cli_version) for p in ks.pins] == [
        ("claude-code", "2.1.229"),
        ("grok-build", "0.2.112"),
        ("grok-build", "1.0.4"),
    ]


def test_unknown_template_is_refused(tmp_path):
    factory = _load_factory()
    dest = tmp_path / "harness_keep_set.json"
    dest.write_text(json.dumps({
        "templates": ["nginx"],
        "pins": [{"template": "nginx", "cli_version": "1.2.3"}],
    }))
    with pytest.raises(factory.InvalidKeepSet, match="nginx"):
        factory.load_keep_set(dest)


def test_non_semver_and_latest_are_refused(tmp_path):
    factory = _load_factory()
    dest = tmp_path / "harness_keep_set.json"
    dest.write_text(json.dumps({
        "pins": [{"template": "grok-build", "cli_version": "latest"}],
    }))
    with pytest.raises(factory.InvalidKeepSet, match="latest"):
        factory.load_keep_set(dest)
    dest.write_text(json.dumps({
        "pins": [{"template": "grok-build", "cli_version": "../x"}],
    }))
    with pytest.raises(factory.InvalidKeepSet, match="semver"):
        factory.load_keep_set(dest)


def test_malformed_keep_set_is_refused(tmp_path):
    factory = _load_factory()
    dest = tmp_path / "harness_keep_set.json"
    dest.write_text("not-json")
    with pytest.raises(factory.InvalidKeepSet):
        factory.load_keep_set(dest)
    dest.write_text(json.dumps(["grok-build"]))
    with pytest.raises(factory.InvalidKeepSet):
        factory.load_keep_set(dest)


def test_plan_bakes_skips_ok_receipt_for_this_digest():
    factory = _load_factory()
    ks = factory.KeepSet(pins=(
        factory.Pin("grok-build", "0.2.112"),
        factory.Pin("grok-build", "1.0.4"),
        factory.Pin("claude-code", "2.1.229"),
    ))
    jobs = factory.plan_bakes(
        ks,
        digest="sha256:abc",
        receipts={
            ("grok-build", "0.2.112"): {"ok": True, "digest": "sha256:abc"},
            ("claude-code", "2.1.229"): {"ok": False, "digest": "sha256:abc"},
            ("grok-build", "1.0.4"): {"ok": True, "digest": "sha256:other"},
        },
    )
    assert [(j.template, j.cli_version) for j in jobs] == [
        ("grok-build", "1.0.4"),
        ("claude-code", "2.1.229"),
    ]


def test_plan_bakes_empty_when_every_pin_has_an_ok_receipt():
    factory = _load_factory()
    ks = factory.KeepSet(pins=(factory.Pin("grok-build", "0.2.112"),))
    jobs = factory.plan_bakes(
        ks,
        digest="sha256:abc",
        receipts={("grok-build", "0.2.112"): {"ok": True, "digest": "sha256:abc"}},
    )
    assert jobs == ()


def test_image_ref_house_pin_is_the_tag_only():
    factory = _load_factory()
    house = {"grok-build": "0.2.112", "claude-code": "2.1.229"}
    assert factory.image_ref(
        "grok-build", "0.2.112", tag="latest", house=house,
    ) == "devcake/dev-grok-build:latest"
    assert factory.image_ref(
        "claude-code", "2.1.229", tag="abc123", house=house,
    ) == "devcake/dev-claude-code:abc123"


def test_image_ref_explicit_pin_is_tag_plus_version():
    factory = _load_factory()
    house = {"grok-build": "0.2.112"}
    assert factory.image_ref(
        "grok-build", "1.0.4", tag="latest", house=house,
    ) == "devcake/dev-grok-build:latest-1.0.4"
    assert factory.image_ref(
        "grok-build", "1.0.4", tag="abc123", house=house,
    ) == "devcake/dev-grok-build:abc123-1.0.4"


def test_image_ref_never_leaves_the_devcake_dev_prefix():
    factory = _load_factory()
    with pytest.raises(factory.InvalidKeepSet, match="nginx"):
        factory.image_ref("nginx", "1.2.3", tag="latest", house={})
    ref = factory.image_ref(
        "grok-build", "1.0.4", tag="latest",
        house={"grok-build": "0.2.112"},
    )
    assert ref.startswith("devcake/dev-")
    assert "/" not in ref.removeprefix("devcake/")


def test_bake_argv_overrides_arg_and_tags_the_versioned_image():
    factory = _load_factory()
    argv = factory.bake_argv(
        factory.BakeJob("grok-build", "1.0.4"),
        tag="latest",
        house={"grok-build": "0.2.112"},
    )
    assert argv[:3] == ["docker", "buildx", "bake"]
    assert "grok-build" in argv
    assert "--set" in argv
    assert "grok-build.args.GROK_VERSION=1.0.4" in argv
    assert "grok-build.tags=devcake/dev-grok-build:latest-1.0.4" in argv


def test_bake_argv_house_pin_does_not_override_the_arg():
    factory = _load_factory()
    argv = factory.bake_argv(
        factory.BakeJob("grok-build", "0.2.112"),
        tag="abc123",
        house={"grok-build": "0.2.112"},
    )
    assert argv[:3] == ["docker", "buildx", "bake"]
    assert "grok-build" in argv
    joined = " ".join(argv)
    assert "GROK_VERSION=" not in joined
    assert "grok-build.tags=devcake/dev-grok-build:abc123" in joined


def test_reconcile_virgin_keep_set_writes_idle_and_does_not_bake(tmp_path):
    factory = _load_factory()
    called = []
    status_path = tmp_path / "harness_bake_status.json"
    out = factory.reconcile(
        keep_set_path=tmp_path / "missing.json",
        receipts_dir=tmp_path / "receipts",
        status_path=status_path,
        digest="sha256:abc",
        baker=called.append,
        tag="latest",
        house={"grok-build": "0.2.112"},
    )
    assert called == []
    assert out["state"] == "virgin"
    body = json.loads(status_path.read_text())
    assert body["state"] == "virgin"


def test_reconcile_invalid_keep_set_writes_error_and_does_not_bake(tmp_path):
    factory = _load_factory()
    dest = tmp_path / "harness_keep_set.json"
    dest.write_text(json.dumps({
        "pins": [{"template": "nginx", "cli_version": "1.2.3"}],
    }))
    called = []
    status_path = tmp_path / "harness_bake_status.json"
    out = factory.reconcile(
        keep_set_path=dest,
        receipts_dir=tmp_path / "receipts",
        status_path=status_path,
        digest="sha256:abc",
        baker=called.append,
        tag="latest",
        house={"grok-build": "0.2.112"},
    )
    assert called == []
    assert out["state"] == "error"
    assert "nginx" in out["detail"]


def test_reconcile_bakes_missing_pins_then_is_ready(tmp_path):
    factory = _load_factory()
    dest = tmp_path / "harness_keep_set.json"
    dest.write_text(json.dumps({
        "pins": [
            {"template": "grok-build", "cli_version": "0.2.112"},
            {"template": "grok-build", "cli_version": "1.0.4"},
        ],
    }))
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "grok-build@0.2.112.json").write_text(json.dumps({
        "digest": "sha256:abc", "ok": True,
    }))
    baked = []

    def baker(job):
        baked.append((job.template, job.cli_version))
        (receipts / f"{job.template}@{job.cli_version}.json").write_text(
            json.dumps({"digest": "sha256:abc", "ok": True}))

    status_path = tmp_path / "harness_bake_status.json"
    out = factory.reconcile(
        keep_set_path=dest,
        receipts_dir=receipts,
        status_path=status_path,
        digest="sha256:abc",
        baker=baker,
        tag="latest",
        house={"grok-build": "0.2.112"},
    )
    assert baked == [("grok-build", "1.0.4")]
    assert out["state"] == "ready"
    assert json.loads(status_path.read_text())["state"] == "ready"

    baked.clear()
    again = factory.reconcile(
        keep_set_path=dest,
        receipts_dir=receipts,
        status_path=status_path,
        digest="sha256:abc",
        baker=baker,
        tag="latest",
        house={"grok-build": "0.2.112"},
    )
    assert baked == []
    assert again["state"] == "ready"


def test_arg_names_match_the_app_house_pins():
    factory = _load_factory()
    from devcake.house_pins import DOCKERFILE_ARG, HOUSE_PINS, LAUNCH_SUPPORTED
    assert factory.ARG_NAMES == DOCKERFILE_ARG
    assert factory.KNOWN_TEMPLATES == frozenset(HOUSE_PINS)
    assert factory.LAUNCH_SUPPORTED == LAUNCH_SUPPORTED


def test_house_from_dockerfile_reads_arg_defaults():
    factory = _load_factory()
    text = (
        "ARG CLAUDE_CODE_VERSION=2.1.229\n"
        "ARG GROK_VERSION=0.2.112\n"
        "ARG CODEX_VERSION=0.147.0\n"
    )
    house = factory.house_from_dockerfile(text)
    assert house["grok-build"] == "0.2.112"
    assert house["claude-code"] == "2.1.229"
    assert house["codex"] == "0.147.0"


def test_run_bake_calls_bake_then_probe():
    factory = _load_factory()
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        return type("R", (), {"returncode": 0})()

    factory.run_bake(
        factory.BakeJob("grok-build", "1.0.4"),
        tag="latest",
        house={"grok-build": "0.2.112"},
        receipts_dir="/tmp/receipts",
        digest="sha256:abc",
        repo=Path("/repo"),
        run=run,
    )
    assert calls[0][:4] == ["docker", "buildx", "bake", "grok-build"]
    assert "grok-build.args.GROK_VERSION=1.0.4" in calls[0]
    assert calls[1][0] == "bash"
    assert calls[1][1].endswith("harness_probe/host_probe.sh")
    assert calls[1][2:5] == ["grok-build", "1.0.4",
                             "devcake/dev-grok-build:latest-1.0.4"]
    assert calls[1][5:] == ["/tmp/receipts", "sha256:abc"]


def test_resolve_image_agrees_with_factory_image_ref(monkeypatch):
    factory = _load_factory()
    monkeypatch.setenv("DEVCAKE_TAG", "latest")
    from devcake.config import DevType
    from devcake.harness import resolve_image
    from devcake.house_pins import HOUSE_PINS

    house = DevType(name="implementer", harness_template="grok-build")
    assert resolve_image(house) == factory.image_ref(
        "grok-build", HOUSE_PINS["grok-build"], tag="latest", house=HOUSE_PINS)
    pinned = DevType(name="implementer", harness_template="grok-build",
                     cli_version="1.0.4")
    assert resolve_image(pinned) == factory.image_ref(
        "grok-build", "1.0.4", tag="latest", house=HOUSE_PINS)


def test_up_sh_default_bake_is_control_plane_and_starts_the_baker():
    candidates = [
        Path(__file__).resolve().parents[2] / "up.sh",
        Path("/srv/up.sh"),
    ]
    path = next((p for p in candidates if p.is_file()), None)
    assert path is not None, "up.sh missing — bind /srv/up.sh"
    text = path.read_text()
    assert "docker buildx bake app admin hello" in text
    assert "python3 -m dev_factory" in text
    assert "PYTHONPATH=" in text
    assert "nohup env" in text
    # No new socket-holder service — baker is a host process.


def test_run_bake_skips_probe_for_experimental_and_writes_a_receipt(tmp_path):
    factory = _load_factory()
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        return type("R", (), {"returncode": 0})()

    factory.run_bake(
        factory.BakeJob("pi", "0.84.2"),
        tag="latest",
        house={"pi": "0.84.2"},
        receipts_dir=tmp_path,
        digest="sha256:abc",
        repo=Path("/repo"),
        run=run,
    )
    assert len(calls) == 1
    assert calls[0][:4] == ["docker", "buildx", "bake", "pi"]
    rec = json.loads((tmp_path / "pi@0.84.2.json").read_text())
    assert rec["ok"] is True
    assert rec["digest"] == "sha256:abc"
    assert rec["gated"] is False


def test_classify_app_down_is_not_sentinel():
    """Unread digest used to keep the baker looping. Down must exit."""
    factory = _load_factory()
    assert factory.classify_app(healthy=False, digest=None) == "down"
    assert factory.classify_app(healthy=False, digest="sha256:abc") == "down"
    assert factory.classify_app(
        healthy=True, digest="DEVCAKE_APP_DIGEST_UNSET") == "sentinel"
    assert factory.classify_app(healthy=True, digest=None) == "sentinel"
    assert factory.classify_app(healthy=True, digest="sha256:abc") == "ready"


def test_tick_decision_exits_when_the_app_is_down():
    factory = _load_factory()
    assert factory.tick_decision("down") == "exit"
    assert factory.tick_decision("sentinel") == "heartbeat"
    assert factory.tick_decision("ready") == "reconcile"


def test_write_status_stamps_a_heartbeat(tmp_path):
    factory = _load_factory()
    body = factory.write_status(
        tmp_path / "harness_bake_status.json",
        {"state": "ready", "jobs": [], "detail": ""})
    assert body["heartbeat_at"]
    assert "T" in body["heartbeat_at"]


def test_append_baker_event_is_jsonl(tmp_path):
    factory = _load_factory()
    dest = tmp_path / "harness_baker.jsonl"
    factory.append_baker_event(dest, {"event": "tick", "state": "ready"})
    factory.append_baker_event(dest, {"event": "error", "detail": "nginx"})
    lines = dest.read_text().splitlines()
    assert json.loads(lines[0])["event"] == "tick"
    assert json.loads(lines[1])["event"] == "error"
    assert json.loads(lines[1])["detail"] == "nginx"
