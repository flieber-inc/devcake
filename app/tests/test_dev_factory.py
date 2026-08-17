"""Host factory: keep-set → bake list. App never Docker; host validates.

Public seam: load_keep_set(path) → KeepSet | None;
plan_bakes(keep_set, *, digest, receipts) → tuple[BakeJob, ...];
image_ref(template, cli_version, *, tag, house) → str.

Independent expected values are the planted JSON pins and the literal
image strings. The factory must refuse an invalid keep-set rather than
trust the app file.
"""

from __future__ import annotations

import io
import json
import sys
import threading
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


def test_plan_bakes_skips_any_receipt_for_this_digest():
    """A receipt for this digest is done work — ok or not. Rebake only
    when the tree id moved or there is no receipt."""
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
    ]


def test_claim_inbox_renames_then_release_deletes(tmp_path):
    factory = _load_factory()
    dest = tmp_path / "harness_keep_set.json"
    dest.write_text('{"pins":[]}\n')
    claimed = factory.claim_inbox(dest)
    assert claimed == tmp_path / "harness_keep_set.json.taking"
    assert claimed.is_file()
    assert not dest.is_file()
    factory.release_inbox(claimed)
    assert not claimed.is_file()


def test_claim_inbox_picks_up_a_leftover_taking_file(tmp_path):
    """A crashed baker left .taking; the next baker resumes it."""
    factory = _load_factory()
    taking = tmp_path / "harness_keep_set.json.taking"
    taking.write_text('{"pins":[]}\n')
    claimed = factory.claim_inbox(tmp_path / "harness_keep_set.json")
    assert claimed == taking
    assert taking.is_file()


def test_new_inbox_replaces_a_stale_taking(tmp_path):
    factory = _load_factory()
    dest = tmp_path / "harness_keep_set.json"
    dest.write_text('{"pins":[{"template":"grok-build","cli_version":"0.2.112"}]}\n')
    taking = tmp_path / "harness_keep_set.json.taking"
    taking.write_text('{"pins":[]}\n')
    claimed = factory.claim_inbox(dest)
    assert claimed.read_text().startswith('{"pins":[{"template"')
    assert not dest.is_file()


def test_prune_without_a_this_tick_keep_set_is_refused():
    """Ephemeral keep-set: prune has no last-good. No order → no rmi."""
    factory = _load_factory()
    assert factory.prune_keep_list(None, tag="latest", house={}) is None
    empty = factory.KeepSet(pins=())
    assert factory.prune_keep_list(empty, tag="latest", house={}) is None


def test_failed_image_listing_does_not_drop_receipts(tmp_path):
    """A dead docker CLI must not look like 'zero images'."""
    factory = _load_factory()
    receipts = tmp_path / "harness_receipts"
    receipts.mkdir()
    dest = receipts / "grok-build@0.2.112.json"
    dest.write_text(json.dumps({
        "ok": True, "digest": "sha256:abc",
        "template": "grok-build", "cli_version": "0.2.112",
    }))
    dropped = factory.drop_receipts_missing_images(
        receipts,
        local_images=None,
        tag="latest",
        house={"grok-build": "0.2.112"},
    )
    assert dropped == ()
    assert dest.is_file()


def test_ok_receipt_for_a_gone_image_is_dropped_and_the_pin_rebakes(tmp_path):
    """Images are the registrar. A leftover ok receipt after docker rmi
    must not keep the pin staffed or skip the rebake."""
    factory = _load_factory()
    receipts = tmp_path / "harness_receipts"
    receipts.mkdir()
    dest = receipts / "grok-build@0.2.112.json"
    dest.write_text(json.dumps({
        "ok": True, "digest": "sha256:abc", "gated": True,
        "template": "grok-build", "cli_version": "0.2.112",
    }))
    dropped = factory.drop_receipts_missing_images(
        receipts,
        local_images=(),
        tag="latest",
        house={"grok-build": "0.2.112"},
    )
    assert dropped == ("grok-build@0.2.112",)
    assert not dest.is_file()
    ks = factory.KeepSet(pins=(factory.Pin("grok-build", "0.2.112"),))
    jobs = factory.plan_bakes(ks, digest="sha256:abc", receipts={})
    assert [(j.template, j.cli_version) for j in jobs] == [
        ("grok-build", "0.2.112")]
    from devcake.adapters.files.receipts import FileReceiptStore
    from devcake.config import DevType
    from devcake.staffing import HarnessNotStaffed, require_staffed
    with pytest.raises(HarnessNotStaffed, match="no receipt"):
        require_staffed(
            DevType(name="d", harness_template="grok-build"),
            digest="sha256:abc",
            store=FileReceiptStore(receipts),
            baker_alive=True)

def test_receipt_stays_when_the_named_image_is_still_local(tmp_path):
    factory = _load_factory()
    receipts = tmp_path / "harness_receipts"
    receipts.mkdir()
    dest = receipts / "grok-build@0.2.112.json"
    dest.write_text(json.dumps({
        "ok": True, "digest": "sha256:abc",
        "template": "grok-build", "cli_version": "0.2.112",
    }))
    dropped = factory.drop_receipts_missing_images(
        receipts,
        local_images=("devcake/dev-grok-build:latest",),
        tag="latest",
        house={"grok-build": "0.2.112"},
    )
    assert dropped == ()
    assert dest.is_file()


def test_plan_bakes_empty_when_every_pin_has_a_receipt_for_this_digest():
    factory = _load_factory()
    ks = factory.KeepSet(pins=(factory.Pin("grok-build", "0.2.112"),))
    jobs = factory.plan_bakes(
        ks,
        digest="sha256:abc",
        receipts={("grok-build", "0.2.112"): {"ok": True, "digest": "sha256:abc"}},
    )
    assert jobs == ()


def test_receipt_fail_detail_names_required_rows_that_did_not_pass():
    factory = _load_factory()
    rec = {
        "ok": False,
        "rows": [
            {"name": "healthy", "required": True, "status": "fail"},
            {"name": "http_401", "required": True, "status": "pass"},
            {"name": "empty", "required": True, "status": "fail"},
            {"name": "plan_mode", "required": True, "status": "pass"},
            {"name": "resume", "required": True, "status": "error",
             "detail": "first invocation exposed no session identity"},
        ],
    }
    job = factory.BakeJob("grok-build", "1.0.4")
    text = factory.receipt_fail_detail(job, rec)
    assert "healthy fail" in text
    assert "empty fail" in text
    assert "resume error" in text
    assert "first invocation exposed no session identity" in text
    assert "http_401" not in text
    assert "plan_mode" not in text
    assert '{"ok": false' not in text


def test_run_bake_probe_failure_uses_receipt_rows_not_stdout_json(tmp_path):
    factory = _load_factory()
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    rec = {
        "ok": False,
        "digest": "sha256:abc",
        "rows": [
            {"name": "healthy", "required": True, "status": "fail"},
            {"name": "resume", "required": True, "status": "error",
             "detail": "first invocation exposed no session identity"},
        ],
    }

    def run(argv, **kw):
        if argv and argv[0] == "docker":
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        dest = receipts / "grok-build@1.0.4.json"
        dest.write_text(json.dumps(rec))
        return type("R", (), {
            "returncode": 1,
            "stdout": '{"ok": false, "path": "/data/harness_receipts/grok-build@1.0.4.json"}\n',
            "stderr": "",
        })()

    with pytest.raises(RuntimeError) as caught:
        factory.run_bake(
            factory.BakeJob("grok-build", "1.0.4"),
            tag="latest",
            house={"grok-build": "0.2.112"},
            receipts_dir=receipts,
            digest="sha256:abc",
            repo=Path("/repo"),
            run=run,
        )
    msg = str(caught.value)
    assert "healthy fail" in msg
    assert "resume error" in msg
    assert '{"ok": false' not in msg
    assert (receipts / "grok-build@1.0.4.json").is_file()


def test_plan_prune_keeps_pins_running_and_hello_and_ignores_nginx():
    factory = _load_factory()
    gone = factory.plan_prune(
        keep_images=("devcake/dev-grok-build:latest-1.0.4",
                     "devcake/dev-hello:latest"),
        running_images=("devcake/dev-claude-code:latest", "nginx"),
        local_images=(
            "devcake/dev-grok-build:latest-1.0.4",
            "devcake/dev-hello:latest",
            "devcake/dev-claude-code:latest",
            "devcake/dev-qwen-code:latest",
            "nginx",
        ),
    )
    assert gone == ("devcake/dev-qwen-code:latest",)


def test_plan_prune_skips_dangling_none_tags_instead_of_aborting():
    factory = _load_factory()
    gone = factory.plan_prune(
        keep_images=("devcake/dev-hello:latest",),
        running_images=("devcake/dev-hello:<none>",),
        local_images=(
            "devcake/dev-hello:latest",
            "devcake/dev-hello:<none>",
            "devcake/dev-qwen-code:latest",
            "<none>:<none>",
        ),
    )
    assert gone == ("devcake/dev-qwen-code:latest",)


def test_run_prune_is_docker_rmi_of_the_planned_refs_only():
    factory = _load_factory()
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    factory.run_prune(("devcake/dev-qwen-code:latest",), run=run)
    assert calls == [["docker", "rmi", "devcake/dev-qwen-code:latest"]]
    with pytest.raises(factory.InvalidKeepSet, match="nginx"):
        factory.run_prune(("nginx",), run=run)
    assert calls == [["docker", "rmi", "devcake/dev-qwen-code:latest"]]


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


def test_reconcile_bakes_missing_pins_together(tmp_path):
    """Two missing pins must overlap. A barrier times out if they run in series."""
    factory = _load_factory()
    dest = tmp_path / "harness_keep_set.json"
    dest.write_text(json.dumps({
        "pins": [
            {"template": "grok-build", "cli_version": "1.0.4"},
            {"template": "claude-code", "cli_version": "2.1.229"},
        ],
    }))
    house = {"grok-build": "0.2.112", "claude-code": "2.1.229"}
    ran = []
    gate = threading.Barrier(2, timeout=2)

    def baker(job):
        ran.append(job.cli_version)
        gate.wait()

    out = factory.reconcile(
        keep_set_path=dest,
        receipts_dir=tmp_path / "receipts",
        status_path=tmp_path / "harness_bake_status.json",
        digest="sha256:abc",
        baker=baker,
        tag="latest",
        house=house,
    )
    assert sorted(ran) == ["1.0.4", "2.1.229"]
    assert out["state"] == "ready"
    assert {j["cli_version"]: j["state"] for j in out["jobs"]} == {
        "1.0.4": "ok", "2.1.229": "ok",
    }


def test_reconcile_finishes_every_job_when_one_fails(tmp_path):
    factory = _load_factory()
    dest = tmp_path / "harness_keep_set.json"
    dest.write_text(json.dumps({
        "pins": [
            {"template": "grok-build", "cli_version": "1.0.4"},
            {"template": "claude-code", "cli_version": "2.1.229"},
        ],
    }))
    house = {"grok-build": "0.2.112", "claude-code": "2.1.229"}
    ran = []
    gate = threading.Barrier(2, timeout=2)

    def baker(job):
        ran.append(job.cli_version)
        gate.wait()
        if job.cli_version == "1.0.4":
            raise RuntimeError("layer failed")

    out = factory.reconcile(
        keep_set_path=dest,
        receipts_dir=tmp_path / "receipts",
        status_path=tmp_path / "harness_bake_status.json",
        digest="sha256:abc",
        baker=baker,
        tag="latest",
        house=house,
    )
    assert sorted(ran) == ["1.0.4", "2.1.229"]
    assert out["state"] == "error"
    assert "layer failed" in out["detail"]
    assert {j["cli_version"]: j["state"] for j in out["jobs"]} == {
        "1.0.4": "error", "2.1.229": "ok",
    }


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


def test_run_bake_includes_child_stderr_in_the_error():
    factory = _load_factory()

    def run(argv, **kw):
        return type("R", (), {
            "returncode": 1, "stdout": "", "stderr": "layer failed xyz",
        })()

    with pytest.raises(RuntimeError, match="layer failed xyz"):
        factory.run_bake(
            factory.BakeJob("grok-build", "1.0.4"),
            tag="latest",
            house={"grok-build": "0.2.112"},
            receipts_dir="/tmp/receipts",
            digest="sha256:abc",
            repo=Path("/repo"),
            run=run,
        )


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
    assert "nohup python3 -m dev_factory" in text
    nohup_line = next(l for l in text.splitlines() if "nohup python3" in l)
    assert "OO_INGEST_PASSWORD" not in nohup_line
    init = next((p / "dev_factory" / "__init__.py"
                 for p in _FACTORY_CANDIDATES if p.is_dir()), None)
    assert init is not None
    body = init.read_text()
    assert body.index("sys.path.insert") < body.index("from .core import")


def test_tee_run_keeps_a_tail_and_writes_through():
    factory = _load_factory()
    from dev_factory.run import tee_run
    sink = io.StringIO()
    got = tee_run(
        ["/bin/sh", "-c", "echo bake-failed-detail >&2; exit 7"],
        stamp=lambda: None, sleep=lambda _s: None, interval=0, sink=sink)
    assert got.returncode == 7
    assert "bake-failed-detail" in got.stdout
    assert "bake-failed-detail" in sink.getvalue()


def test_tee_run_bounds_memory_to_the_tail():
    _load_factory()
    from dev_factory.run import tee_run
    sink = io.StringIO()
    got = tee_run(
        ["/bin/sh", "-c", "i=0; while [ $i -lt 400 ]; do echo xxxxxxxxxx; i=$((i+1)); done"],
        stamp=lambda: None, sleep=lambda _s: None, interval=0,
        sink=sink, tail=40)
    assert len(got.stdout) <= 50
    assert got.returncode == 0


def test_skip_reconcile_only_when_idle_and_mtimes_are_known():
    _load_factory()
    from dev_factory.watch import skip_reconcile
    assert skip_reconcile(
        state="ready", trees=1.0, keep=2.0,
        last_trees=1.0, last_keep=2.0) is True
    assert skip_reconcile(
        state="virgin", trees=1.0, keep=2.0,
        last_trees=1.0, last_keep=2.0) is True
    assert skip_reconcile(
        state="ready", trees=1.1, keep=2.0,
        last_trees=1.0, last_keep=2.0) is False
    assert skip_reconcile(
        state="baking", trees=1.0, keep=2.0,
        last_trees=1.0, last_keep=2.0) is False
    # no inbox (keep is None) — nothing to honor; do not loop
    assert skip_reconcile(
        state="ready", trees=1.0, keep=None,
        last_trees=1.0, last_keep=None) is True
    assert skip_reconcile(
        state="ready", trees=1.0, keep=None,
        last_trees=1.0, last_keep=2.0) is True


def test_host_probe_does_not_mount_the_data_volume():
    candidates = [
        Path(__file__).resolve().parents[2] / "scripts" / "harness_probe" / "host_probe.sh",
        Path("/srv/repo-scripts/harness_probe/host_probe.sh"),
    ]
    path = next((p for p in candidates if p.is_file()), None)
    assert path is not None
    text = path.read_text()
    assert "DEVCAKE_RECEIPTS_VOLUME" not in text
    assert "alpine" not in text


def test_host_probe_pythonpath_includes_image_root_for_devcake_dev():
    """Probe runs in the baked image; aim lives at /devcake_dev (not under scripts)."""
    candidates = [
        Path(__file__).resolve().parents[2] / "scripts" / "harness_probe" / "host_probe.sh",
        Path("/srv/repo-scripts/harness_probe/host_probe.sh"),
    ]
    path = next((p for p in candidates if p.is_file()), None)
    assert path is not None
    text = path.read_text()
    # Must put image root on PYTHONPATH so `import devcake_dev` resolves.
    assert "PYTHONPATH=/:/opt/devcake-scripts" in text or (
        "PYTHONPATH=/" in text and "opt/devcake-scripts" in text)


def test_run_bake_probes_every_registry_template(tmp_path):
    """Compile then probe. No template writes an ungated compile-only receipt."""
    factory = _load_factory()
    from devcake.house_pins import HOUSE_PINS

    for template, version in HOUSE_PINS.items():
        calls = []

        def run(argv, **kw):
            calls.append(list(argv))
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        dest = tmp_path / f"{template}@{version}.json"
        if dest.exists():
            dest.unlink()
        factory.run_bake(
            factory.BakeJob(template, version),
            tag="latest",
            house=dict(HOUSE_PINS),
            receipts_dir=tmp_path,
            digest="sha256:abc",
            repo=Path("/repo"),
            run=run,
        )
        assert calls[0][:4] == ["docker", "buildx", "bake", template]
        assert any("host_probe.sh" in str(part) for part in calls[1]), template
        assert not dest.exists(), f"{template} must not write a compile-only receipt"


def test_image_ref_is_one_rule_for_app_and_baker():
    """Walk the same pins through house_pins.image_ref, factory.image_ref,
    and resolve_image. Independent expected: the literals in CASES."""
    factory = _load_factory()
    from devcake.config import DevType
    from devcake.harness import resolve_image
    from devcake.house_pins import HOUSE_PINS, image_ref as app_ref

    cases = (
        ("grok-build", "", "latest", "devcake/dev-grok-build:latest"),
        ("grok-build", "0.2.112", "latest", "devcake/dev-grok-build:latest"),
        ("grok-build", "1.0.4", "latest", "devcake/dev-grok-build:latest-1.0.4"),
        ("claude-code", "2.1.229", "abc123", "devcake/dev-claude-code:abc123"),
        ("claude-code", "2.1.250", "abc123",
         "devcake/dev-claude-code:abc123-2.1.250"),
    )
    for template, pin, tag, expected in cases:
        assert app_ref(template, pin, tag=tag) == expected, (template, pin)
        effective = pin or HOUSE_PINS[template]
        assert factory.image_ref(
            template, effective, tag=tag, house=HOUSE_PINS) == expected
        if tag == "latest":
            dt = DevType(name="n", harness_template=template, cli_version=pin)
            assert resolve_image(dt) == expected, (template, pin)


def test_classify_app_down_is_not_sentinel():
    """Unread digest used to keep the baker looping. Down must exit."""
    factory = _load_factory()
    assert factory.classify_app(healthy=False, digest=None) == "down"
    assert factory.classify_app(healthy=False, digest="sha256:abc") == "down"
    assert factory.classify_app(
        healthy=True, digest="DEVCAKE_APP_DIGEST_UNSET") == "sentinel"
    assert factory.classify_app(healthy=True, digest=None) == "sentinel"
    assert factory.classify_app(healthy=True, digest="sha256:abc") == "ready"
    assert factory.classify_app(
        healthy=True, digest="sha256:abc", checkout="sha256:other") == "mismatch"
    assert factory.classify_app(
        healthy=True, digest="sha256:abc", checkout="sha256:abc") == "ready"


def test_tick_decision_exits_when_the_app_is_down():
    factory = _load_factory()
    assert factory.tick_decision("down") == "exit"
    assert factory.tick_decision("sentinel") == "heartbeat"
    assert factory.tick_decision("mismatch") == "heartbeat"
    assert factory.tick_decision("ready") == "reconcile"


def test_unhealthy_needs_three_strikes():
    factory = _load_factory()
    assert factory.UNHEALTHY_NEED == 3
    assert factory.unhealthy_verdict(1) is False
    assert factory.unhealthy_verdict(2) is False
    assert factory.unhealthy_verdict(3) is True


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
