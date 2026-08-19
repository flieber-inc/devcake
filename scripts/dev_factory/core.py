"""Keep-set parse, bake plan, image names.

Public seam used by the watch loop and by unit tests. Docker is not imported
here — naming an image is not running one.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

_STATUS_LOCK = threading.Lock()

# Bake-images targets minus hello. Must stay equal to HARNESSES keys
# (ratchet in test_harness_cli_pins / factory tests).
KNOWN_TEMPLATES = frozenset({
    "claude-code",
    "codex",
    "grok-build",
    "pi",
    "opencode",
    "qwen-code",
})

# Single source: app/devcake/house_pins.py (PYTHONPATH includes app/).
from devcake.house_pins import LAUNCH_SUPPORTED  # re-exported for factory tests
from devcake.versions import CLI_VERSION_SEMVER_RE as _SEMVER

_TEMPLATE = re.compile(r"[a-z0-9-]+")
_IMAGE_PREFIX = "devcake/dev-"
_IMAGE_REF = re.compile(r"devcake/dev-[a-z0-9-]+:[A-Za-z0-9._-]+")

# Bake ARG names — ratchet against app.house_pins.DOCKERFILE_ARG.
ARG_NAMES: dict[str, str] = {
    "claude-code": "CLAUDE_CODE_VERSION",
    "codex": "CODEX_VERSION",
    "grok-build": "GROK_VERSION",
    "pi": "PI_VERSION",
    "opencode": "OPENCODE_VERSION",
    "qwen-code": "QWEN_CODE_VERSION",
}


class InvalidKeepSet(ValueError):
    """Keep-set is missing, malformed, or names something we will not bake."""


@dataclass(frozen=True)
class Pin:
    template: str
    cli_version: str


@dataclass(frozen=True)
class KeepSet:
    pins: tuple[Pin, ...]


@dataclass(frozen=True)
class BakeJob:
    template: str
    cli_version: str


TAKING_SUFFIX = ".taking"


def claim_inbox(path: Path | str) -> Path | None:
    """Atomically take an inbox file. Leftover `.taking` is resume.

    A fresh inbox replaces a stale `.taking` (newer desired set wins).
    None = nothing to honor this tick.
    """
    dest = Path(path)
    taking = dest.with_name(dest.name + TAKING_SUFFIX)
    if dest.is_file():
        dest.replace(taking)
        return taking
    if taking.is_file():
        return taking
    return None


def release_inbox(path: Path | str) -> None:
    """Delete a claimed inbox after it has been honored."""
    dest = Path(path)
    try:
        dest.unlink()
    except FileNotFoundError:
        return


def load_keep_set(path: Path | str | None) -> KeepSet | None:
    """None = virgin host (file absent). Anything unreadable or untrusted raises."""
    if path is None:
        return None
    dest = Path(path)
    if not dest.is_file():
        return None
    try:
        raw = dest.read_text()
    except OSError as exc:
        raise InvalidKeepSet(f"cannot read keep-set: {exc}") from exc
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidKeepSet(f"keep-set is not JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise InvalidKeepSet("keep-set must be a JSON object")
    pins_raw = body.get("pins")
    if pins_raw is None:
        raise InvalidKeepSet("keep-set is missing pins")
    if not isinstance(pins_raw, list):
        raise InvalidKeepSet("keep-set pins must be a list")
    seen: set[tuple[str, str]] = set()
    pins: list[Pin] = []
    for item in pins_raw:
        pin = _parse_pin(item)
        key = (pin.template, pin.cli_version)
        if key in seen:
            continue
        seen.add(key)
        pins.append(pin)
    return KeepSet(pins=tuple(pins))


def _parse_pin(item: object) -> Pin:
    if not isinstance(item, dict):
        raise InvalidKeepSet("each pin must be an object")
    # Image names are derived on the host from template + cli_version + tag.
    # A crafted pin that carries them is refused, not silently ignored.
    for banned in ("image", "docker_image"):
        if banned in item:
            raise InvalidKeepSet(f"pin must not carry {banned!r}")
    template = item.get("template")
    version = item.get("cli_version")
    if not isinstance(template, str) or not template:
        raise InvalidKeepSet("pin is missing template")
    if not isinstance(version, str) or not version:
        raise InvalidKeepSet("pin is missing cli_version")
    if template not in KNOWN_TEMPLATES or not _TEMPLATE.fullmatch(template):
        raise InvalidKeepSet(f"unknown template {template!r}")
    if version.lower() == "latest":
        raise InvalidKeepSet("cli_version cannot be 'latest'")
    if not _SEMVER.fullmatch(version):
        raise InvalidKeepSet(f"cli_version must be a semver, got {version!r}")
    return Pin(template=template, cli_version=version)


def plan_bakes(
    keep_set: KeepSet,
    *,
    digest: str,
    receipts: Mapping[tuple[str, str], Mapping],
) -> tuple[BakeJob, ...]:
    """Pins that do not already have a receipt for this app digest.

    A receipt is the bake verb's result — ok or not. Rebake only when the
    tree id moved or the pin has never been baked.
    """
    jobs: list[BakeJob] = []
    for pin in keep_set.pins:
        rec = receipts.get((pin.template, pin.cli_version))
        if rec is not None and rec.get("digest") == digest:
            continue
        jobs.append(BakeJob(template=pin.template, cli_version=pin.cli_version))
    return tuple(jobs)


def receipt_path(receipts_dir: Path | str, job: BakeJob) -> Path:
    return Path(receipts_dir) / f"{job.template}@{job.cli_version}.json"


def _require_dev_image(ref: str) -> str:
    if not _IMAGE_REF.fullmatch(ref):
        raise InvalidKeepSet(f"refusing image name {ref!r}")
    return ref


def prune_keep_list(
    keep_set: KeepSet | None,
    *,
    tag: str,
    house: Mapping[str, str],
) -> tuple[str, ...] | None:
    """Desired images for a prune tick. None = refuse (no order this tick)."""
    if keep_set is None or not keep_set.pins:
        return None
    keep = [f"devcake/dev-hello:{tag}"]
    for pin in keep_set.pins:
        keep.append(image_ref(
            pin.template, pin.cli_version, tag=tag, house=house))
    return tuple(keep)


def plan_prune(
    *,
    keep_images: tuple[str, ...] | list[str],
    running_images: tuple[str, ...] | list[str],
    local_images: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    """Local `devcake/dev-*` images that no pin, running container, or hello needs.

    Only well-formed `devcake/dev-name:tag` refs are keep or delete
    candidates. `nginx`, dangling `<none>` tags, and bare ids are ignored.
    """
    keep: set[str] = set()
    for ref in (*keep_images, *running_images):
        if _IMAGE_REF.fullmatch(str(ref)):
            keep.add(ref)
    gone: list[str] = []
    for ref in local_images:
        if not _IMAGE_REF.fullmatch(str(ref)):
            continue
        if ref not in keep:
            gone.append(ref)
    return tuple(sorted(set(gone)))


def run_prune(
    refs: tuple[str, ...] | list[str],
    *,
    run: Callable[..., object],
) -> None:
    """docker rmi the planned refs. Empty list is a no-op."""
    safe = [_require_dev_image(ref) for ref in refs]
    if not safe:
        return
    result = run(["docker", "rmi", *safe], check=False, capture_output=True,
                 text=True)
    code = getattr(result, "returncode", 1)
    if code != 0:
        raise RuntimeError(f"docker rmi exited {code}" + _run_tail(result))


def receipt_fail_detail(job: BakeJob, rec: Mapping) -> str:
    """Operator copy from a not-ok receipt. Body from staffing chokepoint."""
    from devcake.staffing import receipt_fail_reason
    return (
        f"probe {job.template}@{job.cli_version} failed: "
        f"{receipt_fail_reason(rec)}"
    )


def image_ref(
    template: str,
    cli_version: str,
    *,
    tag: str,
    house: Mapping[str, str],
) -> str:
    """House pin → :TAG. Explicit pin → :TAG-cli_version. Always devcake/dev-*."""
    if template not in KNOWN_TEMPLATES or not _TEMPLATE.fullmatch(template):
        raise InvalidKeepSet(f"unknown template {template!r}")
    if not tag or "/" in tag or ":" in tag:
        raise InvalidKeepSet(f"refusing image tag {tag!r}")
    if cli_version != house.get(template) and not _SEMVER.fullmatch(cli_version):
        raise InvalidKeepSet(
            f"cli_version must be a semver, got {cli_version!r}")
    # One implementation: house_pins.image_ref. PYTHONPATH includes app/.
    from devcake.house_pins import image_ref as _named
    ref = _named(template, cli_version, tag=tag)
    if not ref.startswith(_IMAGE_PREFIX) or "/" in ref.removeprefix("devcake/"):
        raise InvalidKeepSet(f"refusing image name {ref!r}")
    return ref


def bake_argv(
    job: BakeJob,
    *,
    tag: str,
    house: Mapping[str, str],
) -> list[str]:
    """docker buildx bake invocation for one pin. Does not run it."""
    image = image_ref(job.template, job.cli_version, tag=tag, house=house)
    argv = ["docker", "buildx", "bake", job.template]
    if job.cli_version != house.get(job.template):
        arg = ARG_NAMES[job.template]
        argv.extend(["--set", f"{job.template}.args.{arg}={job.cli_version}"])
    argv.extend(["--set", f"{job.template}.tags={image}"])
    return argv


def drop_receipts_missing_images(
    receipts_dir: Path | str,
    *,
    local_images: tuple[str, ...] | list[str],
    tag: str,
    house: Mapping[str, str],
) -> tuple[str, ...]:
    """Delete receipts whose named image is not on the host.

    The image is the registrar. A leftover ok file after docker rmi is
    not a bake.
    """
    if local_images is None:
        return ()
    present = {str(ref) for ref in local_images if _IMAGE_REF.fullmatch(str(ref))}
    root = Path(receipts_dir)
    if not root.is_dir():
        return ()
    dropped: list[str] = []
    for path in sorted(root.glob("*.json")):
        stem = path.stem
        if "@" not in stem:
            continue
        template, version = stem.split("@", 1)
        if template not in KNOWN_TEMPLATES:
            continue
        try:
            ref = image_ref(template, version, tag=tag, house=house)
        except InvalidKeepSet:
            continue
        if ref in present:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        dropped.append(stem)
    return tuple(dropped)


def load_receipts(receipts_dir: Path | str) -> dict[tuple[str, str], dict]:
    root = Path(receipts_dir)
    if not root.is_dir():
        return {}
    out: dict[tuple[str, str], dict] = {}
    for path in root.glob("*.json"):
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        stem = path.stem
        if "@" not in stem:
            continue
        template, version = stem.split("@", 1)
        out[(template, version)] = rec
    return out


def write_status(path: Path | str, payload: Mapping) -> dict:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _STATUS_LOCK:
        return _write_status_unlocked(dest, dict(payload))


def touch_status(path: Path | str) -> dict:
    """Refresh the heartbeat without dropping jobs or state."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _STATUS_LOCK:
        try:
            body = json.loads(dest.read_text())
            if not isinstance(body, dict):
                body = {}
        except (OSError, json.JSONDecodeError):
            body = {"state": "baking", "jobs": []}
        return _write_status_unlocked(dest, body)


def _write_status_unlocked(dest: Path, payload: Mapping) -> dict:
    body = dict(payload)
    body.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
    from .liveness import stamp_heartbeat
    body = stamp_heartbeat(body)
    text = json.dumps(body, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return body


def reconcile(
    *,
    keep_set_path: Path | str,
    receipts_dir: Path | str,
    status_path: Path | str,
    digest: str,
    baker: Callable[[BakeJob], None],
    tag: str,
    house: Mapping[str, str],
    local_images: tuple[str, ...] | list[str] | None = None,
) -> dict:
    """One watch tick. Baker is injected — this module does not call Docker."""
    if local_images is not None:
        drop_receipts_missing_images(
            receipts_dir, local_images=local_images, tag=tag, house=house)
    try:
        keep_set = load_keep_set(keep_set_path)
    except InvalidKeepSet as exc:
        return write_status(status_path, {
            "state": "error",
            "digest": digest,
            "jobs": [],
            "detail": str(exc),
        })
    if keep_set is None:
        return write_status(status_path, {
            "state": "virgin",
            "digest": digest,
            "jobs": [],
            "detail": "no keep-set — control plane + hello only",
        })
    jobs = plan_bakes(
        keep_set, digest=digest, receipts=load_receipts(receipts_dir))
    if not jobs:
        return write_status(status_path, {
            "state": "ready",
            "digest": digest,
            "jobs": [],
            "detail": "",
        })
    listed = [
        {
            "template": j.template,
            "cli_version": j.cli_version,
            "image": image_ref(j.template, j.cli_version, tag=tag, house=house),
            "state": "baking",
        }
        for j in jobs
    ]
    write_status(status_path, {
        "state": "baking",
        "digest": digest,
        "jobs": listed,
        "detail": "",
    })

    def run_one(i: int, job: BakeJob) -> None:
        err: BaseException | None = None
        try:
            baker(job)
        except Exception as exc:  # noqa: BLE001 — baker is the host verb; any failure is an operator-visible error
            err = exc
        dest = Path(status_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with _STATUS_LOCK:
            if err is None:
                listed[i]["state"] = "ok"
            else:
                listed[i]["state"] = "error"
                listed[i]["detail"] = str(err)
            _write_status_unlocked(dest, {
                "state": "baking",
                "digest": digest,
                "jobs": listed,
                "detail": "",
            })

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futs = [pool.submit(run_one, i, job) for i, job in enumerate(jobs)]
        for fut in as_completed(futs):
            fut.result()
    failed = [row for row in listed if row.get("state") == "error"]
    if failed:
        return write_status(status_path, {
            "state": "error",
            "digest": digest,
            "jobs": listed,
            "detail": str(failed[0].get("detail") or ""),
        })
    return write_status(status_path, {
        "state": "ready",
        "digest": digest,
        "jobs": listed,
        "detail": "",
    })


def house_from_dockerfile(text: str) -> dict[str, str]:
    """ARG defaults are the bake source. Inverse of ARG_NAMES."""
    by_arg = {arg: template for template, arg in ARG_NAMES.items()}
    house: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^ARG ([A-Z_]+)=(\S+)\s*$", line)
        if not match:
            continue
        template = by_arg.get(match.group(1))
        if template:
            house[template] = match.group(2)
    return house


def run_bake(
    job: BakeJob,
    *,
    tag: str,
    house: Mapping[str, str],
    receipts_dir: Path | str,
    digest: str,
    repo: Path | str,
    run: Callable[..., object],
) -> None:
    """Compile the image, then write a receipt. `run` is subprocess.run-shaped."""
    root = Path(repo)
    argv = bake_argv(job, tag=tag, house=house)
    result = run(argv, cwd=str(root), check=False, capture_output=True, text=True)
    code = getattr(result, "returncode", 1)
    if code != 0:
        raise RuntimeError(
            f"bake {job.template}@{job.cli_version} exited {code}"
            + _run_tail(result))
    image = image_ref(job.template, job.cli_version, tag=tag, house=house)
    probe = [
        "bash",
        str(root / "scripts" / "harness_probe" / "host_probe.sh"),
        job.template,
        job.cli_version,
        image,
        str(receipts_dir),
        digest,
    ]
    result = run(probe, cwd=str(root), check=False,
                 capture_output=True, text=True)
    code = getattr(result, "returncode", 1)
    if code != 0:
        dest = receipt_path(receipts_dir, job)
        if dest.is_file():
            try:
                rec = json.loads(dest.read_text())
            except (OSError, json.JSONDecodeError):
                rec = None
            if isinstance(rec, dict):
                raise RuntimeError(receipt_fail_detail(job, rec))
        raise RuntimeError(
            f"probe {job.template}@{job.cli_version} exited {code}"
            + _run_tail(result))


def _run_tail(result: object) -> str:
    tail = getattr(result, "stderr", None) or getattr(result, "stdout", None) or ""
    if isinstance(tail, bytes):
        tail = tail.decode("utf-8", "replace")
    text = str(tail).strip()
    if not text:
        return ""
    return ": " + text[-800:]





