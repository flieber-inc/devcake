"""``devcake prune [--devs] [--dry-run]`` and the image tidy-up
``devcake up --release`` runs after a successful bring-up (ADR-0038 addendum).

Two owners, two paths. DevCake's **control-plane** images (app, admin,
app-test, hello) and **dangling** build leftovers are the CLI's to remove:
it already holds the docker socket for bake and compose. **Dev images**
(``devcake/dev-*`` other than hello) belong to the host baker — the
keep-set is the order and receipts are the registrar — so ``--devs`` asks
the app for the same prune the admin button requests and the baker acts on
its next tick; the CLI never ``rmi``s a Dev image itself. Third-party
images are not ours and are left alone.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import admin_api
from .paths import require_checkout_root

CONTROL_PLANE_REPOS = frozenset({
    "devcake/app", "devcake/admin", "devcake/app-test", "devcake/dev-hello"})


@dataclass
class PrunePlan:
    remove: list[str] = field(default_factory=list)   # image refs / ids to rmi
    keep: list[tuple[str, str]] = field(default_factory=list)   # (ref, why)


def plan_image_prune(images: list[tuple[str, str, str]], *, tag: str,
                     in_use: set[str]) -> PrunePlan:
    """``images`` = (repository, tag, id) rows as ``docker images`` lists
    them; ``in_use`` = refs and ids of every container (running or not).
    Pure — the executor and ``up --release`` share it."""
    plan = PrunePlan()
    for repo, itag, iid in images:
        ref = f"{repo}:{itag}"
        if repo == "<none>" or itag == "<none>":
            if iid in in_use:
                plan.keep.append((iid, "in use"))
            else:
                plan.remove.append(iid)
            continue
        if repo.startswith("devcake/dev-") and repo not in CONTROL_PLANE_REPOS:
            plan.keep.append((ref, "Dev image — the baker's (devcake prune --devs)"))
            continue
        if repo not in CONTROL_PLANE_REPOS:
            plan.keep.append((ref, "not a DevCake image"))
            continue
        if itag == tag:
            plan.keep.append((ref, "current release"))
        elif ref in in_use or iid in in_use:
            plan.keep.append((ref, "in use"))
        else:
            plan.remove.append(ref)
    return plan


def _docker(argv: list[str], root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *argv], cwd=str(root), text=True,
                          capture_output=True, timeout=120)


def list_images(root: Path) -> list[tuple[str, str, str]]:
    proc = _docker(["images", "--format", "{{.Repository}} {{.Tag}} {{.ID}}"], root)
    if proc.returncode != 0:
        raise RuntimeError(f"docker images failed: {(proc.stderr or '').strip()}")
    rows = []
    for ln in proc.stdout.splitlines():
        parts = ln.split()
        if len(parts) == 3:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def images_in_use(root: Path) -> set[str]:
    proc = _docker(["ps", "-a", "--format", "{{.Image}} {{.ImageID}}"], root)
    used: set[str] = set()
    if proc.returncode == 0:
        for ln in proc.stdout.splitlines():
            for tok in ln.split():
                used.add(tok)
                used.add(tok.removeprefix("sha256:")[:12])
    return used


def prune_images(root: Path, *, tag: str, dry_run: bool = False,
                 as_json: bool = False) -> dict:
    """Remove stale control-plane images and dangling leftovers; never a Dev
    image. Returns the receipt the callers print or embed."""
    plan = plan_image_prune(list_images(root), tag=tag, in_use=images_in_use(root))
    removed, failed = [], []
    for ref in plan.remove:
        if dry_run:
            continue
        proc = _docker(["rmi", ref], root)          # never -f: a conflict is a keep
        (removed if proc.returncode == 0 else failed).append(
            ref if proc.returncode == 0 else (ref, (proc.stderr or "").strip()[:120]))
    receipt = {"tag": tag, "dry_run": dry_run,
               "planned": list(plan.remove), "removed": removed,
               "failed": failed,
               "kept": [{"ref": r, "why": w} for r, w in plan.keep
                        if w in ("in use",)]}
    if not as_json:
        if plan.remove:
            if dry_run:
                head = f"would remove {len(plan.remove)}"
            elif failed:
                head = f"removed {len(removed)} of {len(plan.remove)}"
            else:
                head = f"removed {len(removed)}"
            sys.stdout.write(f"── {head} stale image(s) "
                             f"(control plane not on {tag}, dangling): "
                             + ", ".join(plan.remove[:6])
                             + ("…" if len(plan.remove) > 6 else "") + "\n")
        else:
            sys.stdout.write(f"── no stale image to remove (everything on {tag} or in use)\n")
        for ref, why in failed:
            sys.stdout.write(f"   kept {ref}: {why}\n")
    return receipt


def request_dev_prune(root: Path) -> tuple[int, object]:
    """Ask the app for the baker's Dev-image prune — the admin button's
    chokepoint (`POST /api/v1/harness/prune`)."""
    return admin_api.request(root, "POST", "/api/v1/harness/prune")


def run_prune(*, devs: bool = False, dry_run: bool = False,
              as_json: bool = False, repo: Path | None = None) -> int:
    try:
        root = repo or require_checkout_root()
    except FileNotFoundError as exc:
        sys.stderr.write(f"devcake prune: {exc}\n")
        return 3
    from .envfile import parse_env_file
    tag = (parse_env_file(root / ".env").get("DEVCAKE_TAG") or "").strip() \
        if (root / ".env").is_file() else ""
    if not tag:
        from .paths import read_version_pin
        tag = read_version_pin(root) or "latest"
    try:
        receipt = prune_images(root, tag=tag, dry_run=dry_run, as_json=as_json)
    except RuntimeError as exc:
        sys.stderr.write(f"devcake prune: {exc}\n")
        return 4
    payload = {"ok": True, "schema_version": 1, "images": receipt, "devs": None}
    if devs:
        if dry_run:
            payload["devs"] = {"requested": False, "detail": "dry run"}
            if not as_json:
                sys.stdout.write("── would: ask the app for the baker's Dev-image prune\n")
        else:
            try:
                status, body = request_dev_prune(root)
            except admin_api.AdminUnreachable as exc:
                sys.stderr.write(f"devcake prune --devs: {exc}\n")
                payload["devs"] = {"requested": False, "detail": str(exc)}
                payload["ok"] = False
            else:
                ok = status == 200
                payload["devs"] = {"requested": ok, "status": status}
                if not ok:
                    payload["ok"] = False
                if not as_json:
                    sys.stdout.write(
                        "── Dev-image prune requested; the host baker removes Dev "
                        "images outside the keep-set on its next tick\n" if ok else
                        f"── the app refused the Dev-image prune request (HTTP {status}): {body}\n")
    if as_json:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return 0 if payload["ok"] else 4
