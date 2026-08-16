"""Keep-set parse, bake plan, image names.

Public seam used by the watch loop and by unit tests. Docker is not imported
here — naming an image is not running one.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

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

LAUNCH_SUPPORTED = frozenset({"claude-code", "codex", "grok-build"})

# Same rule as DevType.cli_version — concrete semver only.
_SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.]+)?")
_TEMPLATE = re.compile(r"[a-z0-9-]+")
_IMAGE_PREFIX = "devcake/dev-"

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
    """Pins that do not already have an ok receipt for this app digest."""
    jobs: list[BakeJob] = []
    for pin in keep_set.pins:
        rec = receipts.get((pin.template, pin.cli_version))
        if rec is not None and rec.get("ok") is True and rec.get("digest") == digest:
            continue
        jobs.append(BakeJob(template=pin.template, cli_version=pin.cli_version))
    return tuple(jobs)


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
    if cli_version == house.get(template):
        ref = f"{_IMAGE_PREFIX}{template}:{tag}"
    else:
        if not _SEMVER.fullmatch(cli_version):
            raise InvalidKeepSet(
                f"cli_version must be a semver, got {cli_version!r}")
        ref = f"{_IMAGE_PREFIX}{template}:{tag}-{cli_version}"
    # Belt: the only legal prefix, and no extra slash after the namespace.
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
) -> dict:
    """One watch tick. Baker is injected — this module does not call Docker."""
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
            "state": "pending",
        }
        for j in jobs
    ]
    write_status(status_path, {
        "state": "baking",
        "digest": digest,
        "jobs": listed,
        "detail": "",
    })
    for i, job in enumerate(jobs):
        listed[i]["state"] = "baking"
        write_status(status_path, {
            "state": "baking",
            "digest": digest,
            "jobs": listed,
            "detail": "",
        })
        try:
            baker(job)
        except Exception as exc:  # noqa: BLE001 — baker is the host verb; any failure is an operator-visible error
            listed[i]["state"] = "error"
            listed[i]["detail"] = str(exc)
            return write_status(status_path, {
                "state": "error",
                "digest": digest,
                "jobs": listed,
                "detail": str(exc),
            })
        listed[i]["state"] = "ok"
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
    receipts_volume: str | None = None,
) -> None:
    """Compile the image, then write a receipt. `run` is subprocess.run-shaped."""
    root = Path(repo)
    argv = bake_argv(job, tag=tag, house=house)
    result = run(argv, cwd=str(root), check=False)
    code = getattr(result, "returncode", 1)
    if code != 0:
        raise RuntimeError(f"bake {job.template}@{job.cli_version} exited {code}")
    if job.template not in LAUNCH_SUPPORTED:
        _write_ungated_receipt(
            Path(receipts_dir), job, digest)
        return
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
    env = None
    if receipts_volume:
        env = {**os.environ, "DEVCAKE_RECEIPTS_VOLUME": receipts_volume}
    result = run(probe, cwd=str(root), check=False, env=env)
    code = getattr(result, "returncode", 1)
    if code != 0:
        raise RuntimeError(
            f"probe {job.template}@{job.cli_version} exited {code}")


def _write_ungated_receipt(receipts_dir: Path, job: BakeJob, digest: str) -> None:
    receipts_dir.mkdir(parents=True, exist_ok=True)
    dest = receipts_dir / f"{job.template}@{job.cli_version}.json"
    dest.write_text(json.dumps({
        "digest": digest,
        "template": job.template,
        "cli_version": job.cli_version,
        "ok": True,
        "gated": False,
        "rows": [],
    }) + "\n")


