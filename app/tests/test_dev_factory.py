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
import os
import subprocess
import sys
import threading
import time
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


def test_pin_image_fields_are_refused(tmp_path):
    """A crafted keep-set cannot smuggle image/docker_image past load."""
    factory = _load_factory()
    dest = tmp_path / "harness_keep_set.json"
    dest.write_text(json.dumps({
        "pins": [{
            "template": "grok-build",
            "cli_version": "1.0.4",
            "docker_image": "evil/smuggle:latest",
        }],
    }))
    with pytest.raises(factory.InvalidKeepSet, match="docker_image"):
        factory.load_keep_set(dest)
    dest.write_text(json.dumps({
        "pins": [{
            "template": "grok-build",
            "cli_version": "1.0.4",
            "image": "nginx:latest",
        }],
    }))
    with pytest.raises(factory.InvalidKeepSet, match="image"):
        factory.load_keep_set(dest)


def test_legacy_templates_field_is_ignored_not_trusted(tmp_path):
    """Old keep-sets may still carry templates[]; only pins bake."""
    factory = _load_factory()
    dest = tmp_path / "harness_keep_set.json"
    dest.write_text(json.dumps({
        "templates": ["nginx", "evil"],
        "pins": [{"template": "grok-build", "cli_version": "1.0.4"}],
    }))
    ks = factory.load_keep_set(dest)
    assert [(p.template, p.cli_version) for p in ks.pins] == [
        ("grok-build", "1.0.4"),
    ]


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


def test_receipt_fail_detail_suffix_matches_staffing_reason():
    """Pinned mirror: baker detail body == staffing.receipt_fail_reason."""
    from devcake.staffing import receipt_fail_reason

    factory = _load_factory()
    rec = {
        "ok": False,
        "rows": [
            {"name": "healthy", "required": True, "status": "fail"},
            {"name": "resume", "required": True, "status": "error",
             "detail": "first invocation exposed no session identity"},
        ],
    }
    job = factory.BakeJob("grok-build", "1.0.4")
    detail = factory.receipt_fail_detail(job, rec)
    prefix = f"probe {job.template}@{job.cli_version} failed: "
    assert detail.startswith(prefix)
    assert detail[len(prefix):] == receipt_fail_reason(rec)


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
    from devcake.harness import HARNESSES
    assert factory.ARG_NAMES == DOCKERFILE_ARG
    assert factory.KNOWN_TEMPLATES == frozenset(HOUSE_PINS)
    assert factory.KNOWN_TEMPLATES == frozenset(HARNESSES)
    assert factory.LAUNCH_SUPPORTED == LAUNCH_SUPPORTED


def test_compose_claim_passes_paths_as_argv_not_shell():
    """Watch-loop claim must not interpolate paths into sh -c text."""
    # Same resolve as _load_factory: local tree or CI /srv/repo-scripts mount.
    scripts = next(
        (p for p in _FACTORY_CANDIDATES if (p / "dev_factory" / "watch.py").is_file()),
        None,
    )
    assert scripts is not None, "scripts/dev_factory/watch.py not found"
    text = (scripts / "dev_factory" / "watch.py").read_text()
    assert 'if [ -f "$1" ]; then mv -f "$1" "$2"; fi' in text
    assert "if [ -f {src" not in text
    assert "f\"if [ -f" not in text


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


def _up_sh_text() -> str:
    candidates = [
        Path(__file__).resolve().parents[2] / "up.sh",
        Path("/srv/up.sh"),
    ]
    path = next((p for p in candidates if p.is_file()), None)
    assert path is not None, "up.sh missing — bind /srv/up.sh"
    return path.read_text()


def test_up_sh_default_bake_is_control_plane_and_starts_the_baker():
    text = _up_sh_text()
    assert "docker buildx bake app admin hello" in text
    # Host baker entry is the CLI verb (ADR-0038 Decision 5); deprecated
    # `python -m dev_factory` remains importable but is not the ExecStart.
    assert "devcake baker run" in text
    assert "PYTHONPATH=" in text
    # Detached path: platform-routed supervisors (not bare nohup baker).
    assert "devcake_baker_platform" in text
    assert "devcake_baker_systemd_install" in text
    assert "devcake_baker_launchd_install" in text
    assert "devcake_baker_respawn_install" in text
    assert "nohup python3 -m dev_factory" not in text
    assert "nohup devcake" not in text
    init = next((p / "dev_factory" / "__init__.py"
                 for p in _FACTORY_CANDIDATES if p.is_dir()), None)
    assert init is not None
    body = init.read_text()
    assert body.index("sys.path.insert") < body.index("from .core import")


def _baker_unit_text() -> str:
    candidates = [
        Path(__file__).resolve().parents[2]
        / "scripts" / "systemd" / "devcake-baker.service",
        Path("/srv/repo-scripts/systemd/devcake-baker.service"),
    ]
    path = next((p for p in candidates if p.is_file()), None)
    assert path is not None, "devcake-baker.service missing"
    return path.read_text()


def test_baker_systemd_unit_restarts_on_failure():
    """Unit keeps the baker host-side with Restart=on-failure + backoff."""
    unit = _baker_unit_text()
    assert "Restart=on-failure" in unit
    assert "RestartSec=" in unit
    # ExecStart uses @DEVCAKE@ (resolved to the console-script path at install).
    assert "ExecStart=@DEVCAKE@ baker run" in unit
    assert "@DEVCAKE@" in unit
    assert "ExecStart=" in unit and "-m dev_factory" not in [
        line for line in unit.splitlines() if line.startswith("ExecStart=")
    ][0]
    assert "WorkingDirectory=" in unit
    # Must never mount docker.sock into a container — baker stays on the host.
    assert "docker.sock" not in unit
    assert "Docker" not in unit
    assert "EnvironmentFile=" in unit
    assert "StartLimit" in unit


def test_up_sh_loud_degraded_gap_on_respawn_fallback():
    """When native supervisors are unavailable, degraded path must shout."""
    text = _up_sh_text()
    helper = _baker_host_sh_text()
    combined = text + "\n" + helper
    assert "DEGRADED" in combined or "degraded" in combined
    assert "respawn" in combined.lower()
    # Gap warning must mention linger / launchd so operators know the prefer path.
    assert "linger" in combined.lower() or "launchd" in combined.lower()
    # Linux DEGRADED reason must consult the sharper probe — not only the
    # hard-coded "user systemd unavailable" for every !systemd_available case.
    assert "devcake_baker_linux_degraded_reason" in text
    assert "devcake_baker_linux_degraded_reason" in helper
    assert "devcake_baker_systemd_user_session_missing" in helper


def test_up_sh_persists_devcake_tag_into_env():
    """AUD-004 residual: a process-env pin must survive plain compose up.

    Bake reads process env / HCL only; compose substitutes from .env when the
    shell no longer exports DEVCAKE_TAG. up.sh already upserts DOCKER_GID and
    DEVCAKE_WS_HOST — the image tag pin must do the same.
    """
    text = _up_sh_text()
    assert "upsert_env_var DEVCAKE_TAG" in text
    assert "would upsert DEVCAKE_TAG=" in text
    assert 'export DEVCAKE_TAG="$TAG"' in text
    # Same durable upserts as the host-specific trio operators move between hosts.
    for key in ("DOCKER_GID", "DEVCAKE_WS_HOST", "DEVCAKE_TAG"):
        assert f"upsert_env_var {key}" in text


def test_up_sh_diagnoses_stale_baker_pid_file():
    """Dead watch.pid must be reported and removed before a new baker starts."""
    text = _up_sh_text()
    assert "baker_host.sh" in text
    assert "devcake_baker_prepare_pidfile" in text
    assert "watch.pid" in text
    # Diagnostic + cleanup live in the sourced helper (unit-tested below).
    helper = _baker_host_sh_text()
    assert "stale" in helper.lower()
    assert "watch.pid" in helper or "pidfile" in helper
    assert "rm -f" in helper


def _baker_host_sh_text() -> str:
    candidates = [
        Path(__file__).resolve().parents[2] / "scripts" / "lib" / "baker_host.sh",
        Path("/srv/repo-scripts/lib/baker_host.sh"),
    ]
    path = next((p for p in candidates if p.is_file()), None)
    assert path is not None, "baker_host.sh missing — bind /srv/repo-scripts"
    return path.read_text()


def test_up_sh_confirms_baker_liveness_after_launch():
    """Detached baker must be confirmed alive with log progress after launch."""
    text = _up_sh_text()
    assert "devcake_baker_wait_liveness" in text
    helper = _baker_host_sh_text()
    assert "kill -0" in helper
    assert "watch.log" in helper or "logfile" in helper
    assert "tail" in helper
    assert "launch:" in helper
    assert "pidfile:" in helper
    assert "--foreground-baker" in helper


def test_up_sh_foreground_baker_bypasses_supervisor():
    """--foreground-baker runs `devcake baker run` without detach/unit."""
    text = _up_sh_text()
    assert "--foreground-baker" in text
    # Documented in the header that usage() prints.
    header = "\n".join(text.splitlines()[:25])
    assert "--foreground-baker" in header
    assert "FOREGROUND_BAKER" in text
    assert "baker run" in text
    assert "devcake_baker_resolve_cli" in text
    # Flag gate: foreground path must not wrap the exec in a supervisor.
    exec_idx = text.index("baker run")
    # Prefer the foreground exec site (after FOREGROUND_BAKER gate).
    fg_marker = text.index("FOREGROUND_BAKER")
    exec_idx = text.index("baker run", fg_marker)
    window = text[max(0, exec_idx - 250): exec_idx]
    assert "nohup" not in window
    assert "systemctl" not in window
    assert "launchctl" not in window
    assert "respawn" not in window
    assert "foreground" in window.lower() or "FOREGROUND_BAKER" in text[:exec_idx]


def _baker_host_sh_path() -> Path:
    candidates = [
        Path(__file__).resolve().parents[2] / "scripts" / "lib" / "baker_host.sh",
        Path("/srv/repo-scripts/lib/baker_host.sh"),
    ]
    path = next((p for p in candidates if p.is_file()), None)
    assert path is not None, "baker_host.sh missing — bind /srv/repo-scripts"
    return path


def _run_baker_host_driver(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    """Source baker_host.sh under stubbed kill/sleep/tail (shell-stub idiom)."""
    driver = tmp_path / "driver.sh"
    helper = _baker_host_sh_path()
    driver.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        + body
        + f'\n# shellcheck source=/dev/null\nsource "{helper}"\n'
        + 'main "$@"\n'
    )
    driver.chmod(0o700)
    return subprocess.run(
        ["bash", str(driver)],
        capture_output=True, text=True, cwd=str(tmp_path),
    )


def test_baker_host_systemd_helpers_are_defined():
    """Supervision chokepoint lives in baker_host.sh (not duplicated in up.sh)."""
    helper = _baker_host_sh_text()
    assert "devcake_baker_systemd_available()" in helper
    assert "devcake_baker_systemd_install()" in helper
    assert "devcake_baker_degraded_gap()" in helper
    assert "devcake_baker_respawn_install()" in helper
    assert "Restart=on-failure" not in helper  # unit file owns restart policy
    assert "systemctl --user" in helper
    assert "baker.env" in helper


def test_baker_host_systemd_available_false_without_systemctl(tmp_path):
    result = _run_baker_host_driver(tmp_path, """
command() {
  if [[ "${1:-}" == "-v" && "${2:-}" == "systemctl" ]]; then
    return 1
  fi
  builtin command "$@"
}
main() {
  set +e
  devcake_baker_systemd_available
  echo "rc=$?"
}
""")
    assert result.returncode == 0, result.stderr
    assert "rc=1" in result.stdout


def test_baker_host_clears_stale_pidfile(tmp_path):
    pidfile = tmp_path / "watch.pid"
    pidfile.write_text("999001\n")
    result = _run_baker_host_driver(tmp_path, f"""
kill() {{
  if [[ "${{1:-}}" == "-0" ]]; then return 1; fi
  return 0
}}
main() {{
  devcake_baker_prepare_pidfile "{pidfile}"
}}
""")
    assert result.returncode == 0, result.stderr
    assert "stale" in result.stdout.lower()
    assert "999001" in result.stdout
    assert str(pidfile) in result.stdout
    assert not pidfile.exists()


def test_baker_host_restarts_live_pid(tmp_path):
    pidfile = tmp_path / "watch.pid"
    pidfile.write_text("4242\n")
    killed = tmp_path / "killed"
    result = _run_baker_host_driver(tmp_path, f"""
kill() {{
  if [[ "${{1:-}}" == "-0" ]]; then return 0; fi
  echo "$1" >> "{killed}"
  return 0
}}
sleep() {{ :; }}
main() {{
  devcake_baker_prepare_pidfile "{pidfile}"
}}
""")
    assert result.returncode == 0, result.stderr
    assert "restarting host baker" in result.stdout
    assert "4242" in result.stdout
    assert pidfile.exists()  # still present until new launch overwrites
    assert killed.read_text().strip() == "4242"


def test_baker_host_displace_orphans_kills_foreign_spares_keep_pid(tmp_path):
    """Install sweep SIGTERMs leftover bakers for this factory; spares keep_pid."""
    factory = tmp_path / ".factory"
    factory.mkdir()
    killed = tmp_path / "killed"
    result = _run_baker_host_driver(tmp_path, f"""
kill() {{
  if [[ "${{1:-}}" == "-0" ]]; then return 0; fi
  echo "$1" >> "{killed}"
  return 0
}}
sleep() {{ :; }}
main() {{
  # Override enumerator after source (call-time lookup).
  devcake_baker_list_factory_bakers() {{
    echo "111 01:02:03"
    echo "222 00:00:05"
  }}
  devcake_baker_displace_orphans "{factory}" 222
}}
""")
    assert result.returncode == 0, result.stderr + result.stdout
    assert killed.read_text().strip() == "111"
    assert "pid 111" in result.stdout
    assert "01:02:03" in result.stdout
    assert "displacing leftover host baker" in result.stdout
    assert "222" not in killed.read_text()


def test_baker_host_displace_orphans_kills_all_without_keep_pid(tmp_path):
    """Pre-start sweep with no keep_pid SIGTERMs every matching baker."""
    factory = tmp_path / ".factory"
    factory.mkdir()
    killed = tmp_path / "killed"
    result = _run_baker_host_driver(tmp_path, f"""
kill() {{
  if [[ "${{1:-}}" == "-0" ]]; then return 0; fi
  echo "$1" >> "{killed}"
  return 0
}}
sleep() {{ :; }}
main() {{
  devcake_baker_list_factory_bakers() {{
    echo "333 age-unknown"
    echo "444 00:01:00"
  }}
  devcake_baker_displace_orphans "{factory}"
}}
""")
    assert result.returncode == 0, result.stderr + result.stdout
    assert sorted(killed.read_text().split()) == ["333", "444"]
    assert "pid 333" in result.stdout
    assert "pid 444" in result.stdout
    assert "pre-supervisor sweep" in result.stdout


def test_baker_host_displace_orphans_never_kills_self(tmp_path):
    """Enumerator listing $$ must not SIGTERM the installing shell."""
    factory = tmp_path / ".factory"
    factory.mkdir()
    killed = tmp_path / "killed"
    result = _run_baker_host_driver(tmp_path, f"""
kill() {{
  if [[ "${{1:-}}" == "-0" ]]; then return 0; fi
  echo "$1" >> "{killed}"
  return 0
}}
sleep() {{ :; }}
main() {{
  self="$$"
  devcake_baker_list_factory_bakers() {{
    echo "$self 00:00:01"
    echo "555 00:00:02"
  }}
  devcake_baker_displace_orphans "{factory}"
  echo "self=$self"
}}
""")
    assert result.returncode == 0, result.stderr + result.stdout
    self_pid = [
        line.split("=", 1)[1]
        for line in result.stdout.splitlines()
        if line.startswith("self=")
    ][0]
    assert killed.read_text().strip() == "555"
    assert self_pid not in killed.read_text().split()


def test_up_sh_and_baker_host_wire_displace_orphans():
    """Displace chokepoint is defined once and invoked on install/refresh."""
    helper = _baker_host_sh_text()
    text = _up_sh_text()
    assert "devcake_baker_displace_orphans()" in helper
    assert "devcake_baker_list_factory_bakers()" in helper
    assert "devcake_baker_displace_orphans" in text
    # Pre-start sweep (no keep_pid) near prepare_pidfile.
    prep = text.index("devcake_baker_prepare_pidfile")
    # First displace call in up.sh should be the pre-start sweep.
    first_disp = text.index("devcake_baker_displace_orphans")
    assert first_disp > prep
    # Post-install keep_pid sweeps live in each installer.
    for name in (
        "devcake_baker_systemd_install()",
        "devcake_baker_launchd_install()",
        "devcake_baker_respawn_install()",
    ):
        assert name in helper
    assert helper.count("devcake_baker_displace_orphans") >= 4  # def + 3 installers


def test_baker_targets_factory_env_is_decisive(tmp_path):
    """When DEVCAKE_FACTORY_DIR is set, it alone decides the factory match.

    env=other + cwd=this → not this factory (do not fall through to cwd).
    env=this → match. env unset + cwd=this → match via cwd fallback.
    Exercises targets_factory directly — not a stubbed enumerator.
    """
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    (repo / ".factory").mkdir(parents=True)
    (other / ".factory").mkdir(parents=True)
    factory_abs = str((repo / ".factory").resolve())
    other_abs = str((other / ".factory").resolve())
    repo_abs = str(repo.resolve())
    cmd = "python3 -m dev_factory"
    result = _run_baker_host_driver(tmp_path, f"""
main() {{
  factory_abs="{factory_abs}"
  other_abs="{other_abs}"
  repo_abs="{repo_abs}"
  cmd="{cmd}"
  # env points elsewhere, cwd is this repo → must NOT match this factory.
  if devcake_baker_targets_factory "$cmd" "$other_abs" "$repo_abs" \\
      "$factory_abs" "$repo_abs"; then
    echo "FAIL env_other_cwd_this matched"
  else
    echo "OK env_other_cwd_this"
  fi
  # env names this factory → match regardless of cwd.
  if devcake_baker_targets_factory "$cmd" "$factory_abs" "/tmp" \\
      "$factory_abs" "$repo_abs"; then
    echo "OK env_this"
  else
    echo "FAIL env_this"
  fi
  # env unset: cwd = repo whose .factory is factory_abs → match.
  if devcake_baker_targets_factory "$cmd" "" "$repo_abs" \\
      "$factory_abs" "$repo_abs"; then
    echo "OK cwd_fallback"
  else
    echo "FAIL cwd_fallback"
  fi
  # env unset: cmdline carries absolute factory path → match.
  if devcake_baker_targets_factory \\
      "python3 -m dev_factory --factory $factory_abs" "" "/var/empty" \\
      "$factory_abs" "$repo_abs"; then
    echo "OK cmdline_fallback"
  else
    echo "FAIL cmdline_fallback"
  fi
}}
""")
    assert result.returncode == 0, result.stderr + result.stdout
    out = result.stdout
    assert "OK env_other_cwd_this" in out
    assert "FAIL env_other_cwd_this" not in out
    assert "OK env_this" in out
    assert "OK cwd_fallback" in out
    assert "OK cmdline_fallback" in out


def _pids_from_block(block: str) -> set[str]:
    out: set[str] = set()
    for line in block.splitlines():
        parts = line.split()
        if parts and parts[0].isdigit():
            out.add(parts[0])
    return out


def _stub_dev_factory_pkg(tmp_path: Path) -> Path:
    """Long-lived `python -m dev_factory` stand-in (argv-shaped, sleeps)."""
    stub_root = tmp_path / "stub_pkg"
    (stub_root / "dev_factory").mkdir(parents=True)
    (stub_root / "dev_factory" / "__init__.py").write_text("")
    (stub_root / "dev_factory" / "__main__.py").write_text(
        "import time\ntime.sleep(120)\n"
    )
    return stub_root


def test_baker_list_factory_bakers_respects_decisive_env(tmp_path):
    """Live process: env=other must not list under this factory (real enumerator)."""
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    factory = repo / ".factory"
    other_factory = other / ".factory"
    factory.mkdir(parents=True)
    other_factory.mkdir(parents=True)
    stub_root = _stub_dev_factory_pkg(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(stub_root)}
    env["DEVCAKE_FACTORY_DIR"] = str(other_factory.resolve())
    proc = subprocess.Popen(
        [sys.executable, "-m", "dev_factory"],
        cwd=str(repo),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.15)
        assert proc.poll() is None, "stub baker exited early"
        result = _run_baker_host_driver(tmp_path, f"""
main() {{
  echo "LIST_BEGIN"
  devcake_baker_list_factory_bakers "{factory.resolve()}"
  echo "LIST_END"
  echo "OTHER_BEGIN"
  devcake_baker_list_factory_bakers "{other_factory.resolve()}"
  echo "OTHER_END"
}}
""")
        assert result.returncode == 0, result.stderr + result.stdout
        text = result.stdout
        this_block = text.split("LIST_BEGIN", 1)[1].split("LIST_END", 1)[0]
        other_block = text.split("OTHER_BEGIN", 1)[1].split("OTHER_END", 1)[0]
        child = str(proc.pid)
        assert child not in _pids_from_block(this_block), (
            f"env=other baker pid {child} listed under this factory:\n{text}"
        )
        assert child in _pids_from_block(other_block), (
            f"env=other baker pid {child} missing from other factory list:\n{text}"
        )
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_baker_list_factory_bakers_cwd_fallback_when_env_unset(tmp_path):
    """Live process: env unset + cwd=repo → listed for that repo's factory."""
    repo = tmp_path / "repo"
    factory = repo / ".factory"
    factory.mkdir(parents=True)
    stub_root = _stub_dev_factory_pkg(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(stub_root)}
    env.pop("DEVCAKE_FACTORY_DIR", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "dev_factory"],
        cwd=str(repo),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.15)
        assert proc.poll() is None, "stub baker exited early"
        result = _run_baker_host_driver(tmp_path, f"""
main() {{
  echo "LIST_BEGIN"
  devcake_baker_list_factory_bakers "{factory.resolve()}"
  echo "LIST_END"
}}
""")
        assert result.returncode == 0, result.stderr + result.stdout
        text = result.stdout
        this_block = text.split("LIST_BEGIN", 1)[1].split("LIST_END", 1)[0]
        child = str(proc.pid)
        assert child in _pids_from_block(this_block), (
            f"cwd-only baker pid {child} missing from factory list:\n{text}"
        )
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_baker_host_darwin_cwd_probe_is_wired():
    """Darwin / no-/proc branch must best-effort resolve cwd (not hard-code empty)."""
    helper = _baker_host_sh_text()
    assert "devcake_baker_process_cwd()" in helper
    # The non-/proc path must call the cwd probe (lsof or shared helper).
    # Hard-coded cwd="" with no probe is the CAKE-169 REVIEW reject defect.
    assert "lsof" in helper
    # Comment / matching rule still documents cwd fallback when env unset.
    assert "cwd" in helper.lower()


def test_baker_host_wait_liveness_succeeds_when_log_grows(tmp_path):
    logfile = tmp_path / "watch.log"
    pidfile = tmp_path / "watch.pid"
    logfile.write_text("")
    pidfile.write_text("7\n")
    result = _run_baker_host_driver(tmp_path, f"""
kill() {{
  if [[ "${{1:-}}" == "-0" ]]; then return 0; fi
  return 0
}}
sleep() {{
  echo "dev_factory: watching" >> "{logfile}"
}}
main() {{
  # baseline 0 measured before launch (passed explicitly)
  devcake_baker_wait_liveness 7 "{logfile}" "{pidfile}" "nohup python3 -m dev_factory &" 4 0
}}
""")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "liveness confirmed" in result.stdout


def test_baker_host_wait_liveness_resets_baseline_on_log_shrinkage(tmp_path):
    """Copytruncate mid-wait must reset baseline; post-rotate growth confirms."""
    logfile = tmp_path / "watch.log"
    pidfile = tmp_path / "watch.pid"
    # Oversized pre-upgrade log — baseline is this length before launch.
    old = "OLD" * 1000
    logfile.write_text(old)
    baseline = len(old.encode())
    pidfile.write_text("7\n")
    result = _run_baker_host_driver(tmp_path, f"""
kill() {{
  if [[ "${{1:-}}" == "-0" ]]; then return 0; fi
  return 0
}}
_sleeps=0
sleep() {{
  _sleeps=$((_sleeps + 1))
  if [[ "$_sleeps" -eq 1 ]]; then
    # Copytruncate shrinks below baseline — wait loop resets, does not succeed.
    : > "{logfile}"
  elif [[ "$_sleeps" -eq 2 ]]; then
    # Post-rotate growth past the reset baseline confirms liveness.
    echo "dev_factory: watching keep-set" >> "{logfile}"
  fi
}}
main() {{
  # 6s budget (step=2) so sleep1=shrink + sleep2=growth fit before timeout.
  devcake_baker_wait_liveness 7 "{logfile}" "{pidfile}" "nohup python3 -m dev_factory &" 6 {baseline}
}}
""")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "liveness confirmed" in result.stdout


def test_baker_host_wait_liveness_failure_mentions_rotation_when_log_shrank(tmp_path):
    """Timeout after shrink must name rotation and still tail the live path."""
    logfile = tmp_path / "watch.log"
    pidfile = tmp_path / "watch.pid"
    old = "OLD" * 1000
    logfile.write_text(old)
    baseline = len(old.encode())
    pidfile.write_text("8\n")
    result = _run_baker_host_driver(tmp_path, f"""
kill() {{
  if [[ "${{1:-}}" == "-0" ]]; then return 0; fi
  return 0
}}
_sleeps=0
sleep() {{
  _sleeps=$((_sleeps + 1))
  if [[ "$_sleeps" -eq 1 ]]; then
    : > "{logfile}"   # shrink only — no growth past the reset baseline
  fi
}}
tail() {{ command tail "$@"; }}
main() {{
  set +e
  devcake_baker_wait_liveness 8 "{logfile}" "{pidfile}" "nohup python3 -m dev_factory &" 4 {baseline}
  echo "rc=$?"
}}
""")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "rc=1" in result.stdout
    assert "did not progress its log" in result.stderr
    assert "rotat" in result.stderr.lower()
    assert f"last log lines ({logfile}):" in result.stderr


def test_baker_host_degraded_reason_distinguishes_no_user_session(tmp_path):
    """systemctl present + dead user bus → enable-linger remedy, not generic."""
    # Case A: binary present, user session missing → sharper linger path.
    result_a = _run_baker_host_driver(tmp_path, """
command() {
  if [[ "${1:-}" == "-v" && "${2:-}" == "systemctl" ]]; then
    echo "/usr/bin/systemctl"
    return 0
  fi
  builtin command "$@"
}
systemctl() {
  if [[ "${1:-}" == "--user" && "${2:-}" == "show-environment" ]]; then
    return 1
  fi
  return 0
}
main() {
  set +e
  devcake_baker_systemd_user_session_missing
  echo "missing_rc=$?"
  reason="$(devcake_baker_linux_degraded_reason)"
  echo "reason=$reason"
  # Banner must name enable-linger for this case (stdout+stderr).
  set +e
  devcake_baker_degraded_gap "$reason" 2>banner.err
  echo "banner<<EOF"
  cat banner.err
  echo "EOF"
}
""")
    assert result_a.returncode == 0, result_a.stderr + result_a.stdout
    assert "missing_rc=0" in result_a.stdout
    reason_line = next(
        ln for ln in result_a.stdout.splitlines() if ln.startswith("reason=")
    )
    reason = reason_line[len("reason=") :]
    assert "user systemd unavailable" != reason
    assert "enable-linger" in reason
    assert "enable-linger" in result_a.stdout.lower() or "enable-linger" in result_a.stderr.lower()
    # Banner body (captured) also carries the remedy by name.
    assert "enable-linger" in result_a.stdout

    # Case B: no systemctl at all → generic unavailable; do NOT prescribe linger
    # as the primary reason (banner tip may still mention it).
    result_b = _run_baker_host_driver(tmp_path, """
command() {
  if [[ "${1:-}" == "-v" && "${2:-}" == "systemctl" ]]; then
    return 1
  fi
  builtin command "$@"
}
main() {
  set +e
  devcake_baker_systemd_user_session_missing
  echo "missing_rc=$?"
  reason="$(devcake_baker_linux_degraded_reason)"
  echo "reason=$reason"
}
""")
    assert result_b.returncode == 0, result_b.stderr + result_b.stdout
    assert "missing_rc=1" in result_b.stdout
    assert "reason=user systemd unavailable" in result_b.stdout


def test_up_sh_measures_baker_log_baseline_before_launch():
    """Pre-launch baseline avoids racing the startup print into the check."""
    text = _up_sh_text()
    assert "_BAKER_BASELINE" in text
    # Baseline assignment appears before any supervisor install path.
    base_idx = text.index("_BAKER_BASELINE")
    launch_idx = min(
        text.index("devcake_baker_systemd_install"),
        text.index("devcake_baker_launchd_install"),
        text.index("devcake_baker_respawn_install"),
    )
    assert base_idx < launch_idx
    assert 'devcake_baker_wait_liveness' in text
    assert '"$_BAKER_BASELINE"' in text or "$_BAKER_BASELINE" in text


def test_baker_host_wait_liveness_fails_when_pid_dies(tmp_path):
    logfile = tmp_path / "watch.log"
    pidfile = tmp_path / "watch.pid"
    logfile.write_text("boot\n")
    pidfile.write_text("8\n")
    result = _run_baker_host_driver(tmp_path, f"""
kill() {{
  if [[ "${{1:-}}" == "-0" ]]; then return 1; fi
  return 0
}}
sleep() {{ :; }}
tail() {{ command tail "$@"; }}
main() {{
  set +e
  devcake_baker_wait_liveness 8 "{logfile}" "{pidfile}" "nohup python3 -m dev_factory &" 4
  echo "rc=$?"
}}
""")
    assert result.returncode == 0, result.stderr
    assert "rc=1" in result.stdout
    assert "launch: nohup python3 -m dev_factory &" in result.stderr
    assert f"pidfile: {pidfile}" in result.stderr
    assert "--foreground-baker" in result.stderr


def _baker_host_docker_stub_body(data_root: Path, *, fail: bool = False) -> str:
    """Shell body fragment: stub `docker compose exec -T app` into data_root."""
    root = data_root.as_posix()
    if fail:
        return f"""
docker() {{
  echo "docker stub forced failure: $*" >&2
  return 1
}}
"""
    return f"""
DATA_ROOT="{root}"
docker() {{
  if [[ "${{1:-}}" != "compose" || "${{2:-}}" != "exec" \\
        || "${{3:-}}" != "-T" || "${{4:-}}" != "app" ]]; then
    echo "unexpected docker $*" >&2
    return 1
  fi
  shift 4
  case "${{1:-}}" in
    mkdir)
      local path=""
      for a in "$@"; do
        if [[ "$a" == /data/* ]]; then path="$a"; fi
      done
      [[ -n "$path" ]] || return 1
      mkdir -p "$DATA_ROOT/${{path#/data/}}"
      return 0
      ;;
    tee)
      local dest="${{2:-}}"
      [[ "$dest" == /data/* ]] || return 1
      local rel="${{dest#/data/}}"
      mkdir -p "$(dirname "$DATA_ROOT/$rel")"
      cat > "$DATA_ROOT/$rel"
      return 0
      ;;
    *)
      echo "unexpected docker compose exec app $*" >&2
      return 1
      ;;
  esac
}}
"""


def test_baker_host_liveness_failure_emits_outbox_and_error_status(tmp_path):
    """CAKE-134: launch death ships outbox + honest error status (no heartbeat)."""
    import json

    logfile = tmp_path / "watch.log"
    pidfile = tmp_path / "watch.pid"
    data_root = tmp_path / "container_data"
    data_root.mkdir()
    launch = "nohup python3 -m dev_factory &"
    logfile.write_text(
        "Traceback (most recent call last):\n"
        "  File \"<stdin>\", line 1, in <module>\n"
        "ModuleNotFoundError: No module named 'missing_pkg'\n"
        + "A" * 3000 + "\n"  # single oversized line — the 2000 cap must be total
    )
    pidfile.write_text("8\n")
    stub = _baker_host_docker_stub_body(data_root)
    result = _run_baker_host_driver(tmp_path, f"""
{stub}
kill() {{
  if [[ "${{1:-}}" == "-0" ]]; then return 1; fi
  return 0
}}
sleep() {{ :; }}
tail() {{ command tail "$@"; }}
main() {{
  set +e
  devcake_baker_wait_liveness 8 "{logfile}" "{pidfile}" "{launch}" 4
  echo "rc=$?"
}}
""")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "rc=1" in result.stdout
    assert f"launch: {launch}" in result.stderr
    assert "--foreground-baker" in result.stderr

    outbox = data_root / "harness_outbox"
    assert outbox.is_dir(), result.stderr + result.stdout
    failed = list(outbox.glob("*-launch_failed.jsonl"))
    assert len(failed) == 1, failed
    line = failed[0].read_text().strip()
    assert "\n" not in line
    rec = json.loads(line)
    assert rec["event"] == "launch_failed"
    assert isinstance(rec["ts"], str) and "T" in rec["ts"]
    assert isinstance(rec["detail"], str)
    assert launch in rec["detail"]
    assert "ModuleNotFoundError: No module named 'missing_pkg'" in rec["detail"]
    assert len(rec["detail"]) == 2000  # input exceeds the cap → exact truncation

    status_path = data_root / "harness_bake_status.json"
    assert status_path.is_file(), result.stderr
    status = json.loads(status_path.read_text())
    assert status["state"] == "error"
    assert "heartbeat_at" not in status
    assert str(status["detail"]).startswith("host baker died at launch:")
    assert "ModuleNotFoundError" in status["detail"] or "Traceback" in status["detail"]


def test_baker_host_liveness_failure_emit_is_best_effort(tmp_path):
    """Docker write failures must not mask diagnostics or change rc=1."""
    logfile = tmp_path / "watch.log"
    pidfile = tmp_path / "watch.pid"
    logfile.write_text("boom\n")
    pidfile.write_text("9\n")
    stub = _baker_host_docker_stub_body(tmp_path / "unused", fail=True)
    result = _run_baker_host_driver(tmp_path, f"""
{stub}
kill() {{
  if [[ "${{1:-}}" == "-0" ]]; then return 1; fi
  return 0
}}
sleep() {{ :; }}
tail() {{ command tail "$@"; }}
main() {{
  set +e
  devcake_baker_wait_liveness 9 "{logfile}" "{pidfile}" "nohup python3 -m dev_factory &" 4
  echo "rc=$?"
}}
""")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "rc=1" in result.stdout
    assert "launch: nohup python3 -m dev_factory &" in result.stderr
    assert "--foreground-baker" in result.stderr


def test_up_sh_bake_invokes_hello_dispatch_smoke():
    """CAKE-130: --bake proves Dagu→Dev dispatch via ci_dispatch_hello.sh."""
    text = _up_sh_text()
    assert "ci_dispatch_hello.sh" in text
    assert "--no-hello-smoke" in text
    assert "NO_HELLO_SMOKE" in text


def test_up_sh_hello_smoke_ordered_before_success_banner():
    """Success banner must not print until the bake-path hello smoke finishes."""
    text = _up_sh_text()
    smoke_at = text.index("ci_dispatch_hello.sh")
    success_at = text.index("stack starting")
    assert smoke_at < success_at


def test_up_sh_hello_smoke_reads_admin_without_sourcing_env():
    """ADMIN_* for the smoke come from selective .env parse — never source .env."""
    text = _up_sh_text()
    assert "source .env" not in text
    assert ". .env" not in text
    assert "ADMIN_USER" in text
    assert "ADMIN_PASSWORD" in text
    # Same selective-key family as OO_INGEST_* (grep matching keys + export).
    assert "ADMIN_USER=*" in text or "ADMIN_USER=" in text
    assert "grep -E" in text


def test_up_sh_hello_smoke_failure_points_at_dagu_and_socket():
    """Failed hello gate names diagnostics operators need (dagu logs + sock)."""
    text = _up_sh_text()
    assert "docker compose logs" in text and "dagu" in text
    assert "Docker-socket" in text or "docker socket" in text.lower() or "Docker Desktop" in text


def test_up_sh_validates_oo_passwords_before_bake_and_compose():
    """CAKE-131: OO policy gate must fail before any bake/compose action."""
    text = _up_sh_text()
    assert "require_oo_password OO_ROOT_PASSWORD" in text
    assert "require_oo_password OO_INGEST_PASSWORD" in text
    assert "oo_password.sh" in text
    # Match real action lines (leading indent), not earlier comments that
    # mention the same verbs.
    lines = text.splitlines()
    gate_line = next(
        i for i, line in enumerate(lines)
        if "require_oo_password OO_ROOT_PASSWORD" in line
    )
    bake = next(
        i for i, line in enumerate(lines)
        if line.lstrip().startswith("docker buildx bake")
    )
    compose = next(
        i for i, line in enumerate(lines)
        if line.lstrip().startswith("docker compose up -d")
    )
    assert gate_line < bake
    assert gate_line < compose


def test_up_sh_health_gate_hints_openobserve_logs():
    """Weak OO root password crash-loops OO; the app health gate must name it."""
    text = _up_sh_text()
    # Locate the ~60s live-timeout warning branch (not the redis/dagu probe).
    idx = text.index("app did not report live within")
    branch = text[idx : idx + 400]
    assert "docker compose logs" in branch and "app" in branch
    assert "docker compose logs openobserve" in branch


def test_up_sh_prefers_incontainer_docker_gid_and_gates_socket():
    """CAKE-128: host-stat alone is wrong on Docker Desktop; probe + gate.

    Text contract only — live Desktop acceptance is a residual on Linux agents.
    Exact operator strings are the public seam operators and dry-run share.

    The writability gate must exec as the post-entrypoint credentials (uid 1000
    / gid $DOCKER_GID). The pinned dagu image has empty Config.User, so bare
    ``compose exec`` is root and false-greens a wrong DOCKER_GID.
    """
    text = _up_sh_text()
    assert "devcake_docker_gid_incontainer" in text
    assert "in-container view" in text
    assert "host path says" in text
    assert "in-container probe failed" in text
    assert "docs/14-security.md" in text
    assert "root-group" in text
    # Gate identity must match the running daemon (not root).
    assert 'docker compose exec -T --user "1000:${GID}" dagu' in text
    assert "test -w /var/run/docker.sock" in text
    assert "docker-compose.override.yml" in text
    assert 'DOCKER_GID: "0"' in text


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


def test_keep_set_mtime_discards_stat_stderr(monkeypatch):
    """Empty keep-set is normal idle — compose stat stderr must not leak."""
    _load_factory()
    import dev_factory.watch as watch

    captured: dict = {}

    def fake_check_output(argv, **kwargs):
        captured.update(kwargs)
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(watch.subprocess, "check_output", fake_check_output)
    assert watch.keep_set_mtime() is None
    assert captured.get("stderr") is subprocess.DEVNULL


def test_rotate_watch_log_caps_oversized_log(tmp_path):
    """Oversized watch.log is copytruncated so open redirect FDs keep working."""
    _load_factory()
    import dev_factory.watch as watch

    log = tmp_path / "watch.log"
    # Independent expected: cap is the module literal; payload exceeds it.
    cap = watch.WATCH_LOG_CAP_BYTES
    assert cap == 2 * 1024 * 1024
    log.write_bytes(b"x" * (cap + 100))
    assert watch.rotate_watch_log(log) is True
    assert log.stat().st_size == 0
    bak = tmp_path / "watch.log.1"
    assert bak.is_file()
    assert bak.stat().st_size == cap + 100
    # Under the cap → no-op.
    log.write_bytes(b"ok")
    assert watch.rotate_watch_log(log) is False
    assert log.read_bytes() == b"ok"


def test_unhealthy_budget_is_minutes_not_three_strikes():
    """App-down wait is a minutes-scale budget with backoff — streak=3 is not fatal."""
    factory = _load_factory()
    assert factory.UNHEALTHY_BUDGET_S == 300
    assert factory.UNHEALTHY_BACKOFF_START_S == 5
    assert factory.UNHEALTHY_BACKOFF_CAP_S == 30
    # Three probes must NOT exhaust the budget (old death-during-deploy bug).
    assert factory.unhealthy_verdict(elapsed_s=15) is False
    assert factory.unhealthy_verdict(elapsed_s=299) is False
    assert factory.unhealthy_verdict(elapsed_s=300) is True
    # Backoff grows from START toward CAP.
    assert factory.unhealthy_backoff_s(1) == 5
    assert factory.unhealthy_backoff_s(2) == 10
    assert factory.unhealthy_backoff_s(3) == 20
    assert factory.unhealthy_backoff_s(4) == 30
    assert factory.unhealthy_backoff_s(10) == 30


def test_baker_singleton_flock_blocks_second_instance(tmp_path, capsys):
    """Pidfile + flock: a second baker cannot run while the lock is held.

    Exit code 0 on contention so Restart=on-failure / launchd KeepAlive
    (SuccessfulExit=false) do not restart-storm a healthy peer.
    Contention log must name the holder pid already written into watch.lock.
    """
    _load_factory()
    import dev_factory.watch as watch

    factory_dir = tmp_path / ".factory"
    holder = watch.acquire_baker_singleton(factory_dir)
    assert holder is not None
    pidfile = factory_dir / "watch.pid"
    lockfile = factory_dir / "watch.lock"
    assert pidfile.is_file()
    assert lockfile.is_file()
    holder_pid = lockfile.read_text().strip()
    assert holder_pid.isdigit()
    assert holder_pid == str(os.getpid())

    # Second acquire must refuse without stealing the lock, naming the holder.
    with pytest.raises(SystemExit) as excinfo:
        watch.acquire_baker_singleton(factory_dir)
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert f"pid {holder_pid}" in out
    assert str(lockfile) in out
    assert "exiting without stealing the lock" in out

    holder.close()
    # After release, a new holder can take the lock.
    holder2 = watch.acquire_baker_singleton(factory_dir)
    assert holder2 is not None
    holder2.close()


def _baker_plist_text() -> str:
    candidates = [
        Path(__file__).resolve().parents[2]
        / "scripts" / "launchd" / "com.devcake.baker.plist",
        Path("/srv/repo-scripts/launchd/com.devcake.baker.plist"),
    ]
    path = next((p for p in candidates if p.is_file()), None)
    assert path is not None, "com.devcake.baker.plist missing"
    return path.read_text()


def test_baker_launchd_plist_keepalive_and_run_at_load():
    """macOS LaunchAgent must KeepAlive on non-success and RunAtLoad."""
    plist = _baker_plist_text()
    assert "com.devcake.baker" in plist
    assert "RunAtLoad" in plist
    assert "KeepAlive" in plist
    assert "SuccessfulExit" in plist
    # Runner sources baker.env then execs `devcake baker run` (host-side).
    assert "@RUNNER@" in plist
    assert "baker run" in plist or "devcake" in plist
    assert "WorkingDirectory" in plist or "@REPO@" in plist
    # Must never mount docker.sock — baker stays on the host.
    assert "docker.sock" not in plist


def test_baker_host_platform_and_launchd_helpers_are_defined():
    """Platform routing + launchd install live in baker_host.sh."""
    helper = _baker_host_sh_text()
    assert "devcake_baker_platform()" in helper
    assert "devcake_baker_launchd_available()" in helper
    assert "devcake_baker_launchd_install()" in helper
    assert "launchctl" in helper
    assert "com.devcake.baker" in helper


def test_up_sh_routes_supervisors_by_platform():
    """Detached path: Darwin→launchd, Linux+systemd→unit, else→respawn."""
    text = _up_sh_text()
    helper = _baker_host_sh_text()
    combined = text + "\n" + helper
    assert "devcake_baker_platform" in combined
    assert "devcake_baker_launchd_install" in combined
    assert "devcake_baker_systemd_install" in combined
    assert "devcake_baker_respawn_install" in combined
    # Bare unsupervised nohup of the baker is no longer the fallback.
    assert "devcake_baker_respawn" in combined
    # Password must not appear on a launch argv line.
    for line in text.splitlines():
        if "nohup" in line and "dev_factory" in line:
            assert "OO_INGEST_PASSWORD" not in line


def test_baker_respawn_script_is_flock_guarded_loop():
    """Degraded path: installed respawn loop with flock — not bare nohup baker."""
    candidates = [
        Path(__file__).resolve().parents[2]
        / "scripts" / "lib" / "baker_respawn.sh",
        Path("/srv/repo-scripts/lib/baker_respawn.sh"),
    ]
    path = next((p for p in candidates if p.is_file()), None)
    assert path is not None, "baker_respawn.sh missing"
    body = path.read_text()
    assert "flock" in body
    assert "baker run" in body
    assert "-m dev_factory" not in body
    # Loop / respawn on exit — not a one-shot.
    assert "while" in body or "respawn" in body.lower()
    helper = _baker_host_sh_text()
    assert "devcake_baker_respawn_install()" in helper
    assert "DEGRADED" in helper or "degraded" in helper
    # Generated launchd runner also execs the CLI verb.
    assert "baker run" in helper
    assert 'exec "${python_bin}" -m dev_factory' not in helper


def _patch_once_compose(monkeypatch, listed):
    """Stub host Docker/compose seams so once() is hermetic."""
    import dev_factory.watch as watch

    monkeypatch.setattr(watch, "compose_claim", lambda rel: None)
    monkeypatch.setattr(watch, "compose_read", lambda rel: None)
    monkeypatch.setattr(watch, "compose_ls", lambda rel: [])
    monkeypatch.setattr(watch, "compose_rm", lambda rel: None)
    monkeypatch.setattr(watch, "compose_write", lambda rel, text: None)
    monkeypatch.setattr(
        watch, "docker_name_list",
        lambda argv: list(listed))
    return watch


def test_once_hello_only_images_are_virgin(tmp_path, monkeypatch):
    """Hello is baked by every ./up.sh — it is not evidence of staffing."""
    _load_factory()
    watch = _patch_once_compose(
        monkeypatch,
        ["devcake/dev-hello:latest", "devcake/dev-hello:<none>"])
    status = watch.once(
        work=tmp_path, tag="latest",
        house={"grok-build": "0.2.112"}, digest="sha256:abc")
    assert status["state"] == "virgin"
    assert status["detail"] == "no keep-set — control plane + hello only"


def test_once_real_harness_image_is_ready_without_keep_set(tmp_path, monkeypatch):
    _load_factory()
    watch = _patch_once_compose(
        monkeypatch,
        ["devcake/dev-hello:latest", "devcake/dev-grok-build:0.2.112"])
    status = watch.once(
        work=tmp_path, tag="latest",
        house={"grok-build": "0.2.112"}, digest="sha256:abc")
    assert status["state"] == "ready"
    assert status["detail"] == ""


def test_once_preserves_keep_set_published_during_reconcile(tmp_path, monkeypatch):
    """A keep-set published mid-tick must survive cleanup for the next claim.

    Public seam: watch.once — may remove only the claimed `.taking` inbox,
    never the live harness_keep_set.json publication path.
    """
    _load_factory()
    import dev_factory.watch as watch

    keep_a = json.dumps({
        "pins": [
            {"template": "claude-code", "cli_version": "2.1.229"},
            {"template": "grok-build", "cli_version": "0.2.112"},
        ],
    })
    keep_b = json.dumps({
        "pins": [
            {"template": "codex", "cli_version": "0.149.0"},
            {"template": "claude-code", "cli_version": "2.1.240"},
        ],
    })
    data: dict[str, str] = {watch.KEEP_SET: keep_a}
    seen_keep_bodies: list[str] = []

    def compose_claim(rel: str) -> None:
        if rel in data:
            data[rel + watch.TAKING_SUFFIX] = data.pop(rel)

    def compose_read(rel: str) -> str | None:
        return data.get(rel)

    def compose_write(rel: str, text: str) -> None:
        data[rel] = text

    def compose_rm(rel: str) -> None:
        data.pop(rel, None)

    def compose_ls(rel: str) -> list[str]:
        prefix = rel.rstrip("/") + "/"
        names: list[str] = []
        for key in data:
            if key.startswith(prefix):
                names.append(key[len(prefix):].split("/", 1)[0])
        return names

    def fake_reconcile(*, keep_set_path, **_kw):
        body = Path(keep_set_path).read_text()
        seen_keep_bodies.append(body)
        # App publishes a newer desired set while this claimed tick is in flight.
        data[watch.KEEP_SET] = keep_b
        return {
            "state": "ready",
            "digest": "sha256:abc",
            "jobs": [],
            "detail": "",
        }

    monkeypatch.setattr(watch, "compose_claim", compose_claim)
    monkeypatch.setattr(watch, "compose_read", compose_read)
    monkeypatch.setattr(watch, "compose_write", compose_write)
    monkeypatch.setattr(watch, "compose_rm", compose_rm)
    monkeypatch.setattr(watch, "compose_ls", compose_ls)
    monkeypatch.setattr(
        watch, "docker_name_list",
        lambda argv: ["devcake/dev-hello:latest", "devcake/dev-grok-build:0.2.112"])
    monkeypatch.setattr(watch, "reconcile", fake_reconcile)

    house = {"grok-build": "0.2.112", "claude-code": "2.1.229", "codex": "0.147.0"}
    status = watch.once(
        work=tmp_path, tag="latest", house=house, digest="sha256:abc")
    assert status["state"] == "ready"
    assert data.get(watch.KEEP_SET) == keep_b
    assert watch.KEEP_SET + watch.TAKING_SUFFIX not in data
    assert seen_keep_bodies == [keep_a]

    status = watch.once(
        work=tmp_path, tag="latest", house=house, digest="sha256:abc")
    assert status["state"] == "ready"
    assert seen_keep_bodies == [keep_a, keep_b]
    assert watch.KEEP_SET + watch.TAKING_SUFFIX not in data


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


# Host baker entry (`devcake baker run` / deprecated `python -m dev_factory`)
# runs with PYTHONPATH=scripts:app — no venv for the factory modules.
# Everything watch/core can import, at load or at runtime, must be stdlib or
# first-party. This suite runs in the app image where pydantic IS installed,
# so a plain import cannot catch a leak; the subprocess blocks third-party
# imports to simulate a clean Mac/Debian host (the versions.py → harness.py
# → pydantic edge shipped exactly this way).
_HOST_GUARD = """
import sys

FIRST_PARTY = {"dev_factory", "devcake", "app_digest", "devcake_cli"}

class BlockThirdParty:
    def find_spec(self, name, path=None, target=None):
        top = name.partition(".")[0]
        if top in FIRST_PARTY or top in sys.stdlib_module_names:
            return None
        raise ImportError(f"third-party import on the baker's host path: {name}")

sys.meta_path.insert(0, BlockThirdParty())

import dev_factory            # package load: liveness + core + devcake.versions
import dev_factory.watch      # baker loop (CLI and deprecated -m entry)
import app_digest             # watch.py imports it at runtime
import devcake.staffing       # core.py runtime import (receipt fail detail)
import devcake.bake_status    # staffing's lazy liveness read
print("HOST-CLOSURE-OK")
"""


def test_baker_host_import_closure_is_stdlib_only():
    scripts_root = next((p for p in _FACTORY_CANDIDATES if p.is_dir()), None)
    assert scripts_root is not None, "scripts/ missing — bind scripts → /srv/repo-scripts"
    app_root = Path(__file__).resolve().parents[1]
    env = {**os.environ,
           "PYTHONPATH": f"{scripts_root}{os.pathsep}{app_root}"}
    proc = subprocess.run(
        [sys.executable, "-c", _HOST_GUARD],
        capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, f"baker host closure broke:\n{proc.stderr}"
    assert "HOST-CLOSURE-OK" in proc.stdout
