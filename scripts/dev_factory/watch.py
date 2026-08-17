"""Host baker loop. Started by up.sh — not a compose service.

Reads the keep-set the app published into the /data volume, validates it
independently, compiles + probes, writes receipts and a status file the
app surfaces as baking / ready. The app never talks to Docker.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

from .core import (
    house_from_dockerfile,
    image_ref,
    load_keep_set,
    plan_prune,
    receipt_path,
    reconcile,
    run_bake,
    run_prune,
    touch_status,
    write_status,
)
from .run import tee_run
from .liveness import (
    SENTINEL,
    UNHEALTHY_NEED,
    append_baker_event,
    classify_app,
    tick_decision,
    unhealthy_verdict,
)
INTERVAL = float(os.environ.get("DEVCAKE_FACTORY_INTERVAL", "5"))
KEEP_SET = "harness_keep_set.json"
STATUS = "harness_bake_status.json"
RECEIPTS = "harness_receipts"
BAKER_LOG = "harness_baker.jsonl"
PRUNE_REQUEST = "harness_prune_request.json"


def compose_read(rel: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["docker", "compose", "exec", "-T", "app", "cat", f"/data/{rel}"],
            cwd=REPO, text=True, timeout=15,
            stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return out


def compose_ls(rel: str) -> list[str]:
    try:
        out = subprocess.check_output(
            ["docker", "compose", "exec", "-T", "app", "ls", "-1", f"/data/{rel}"],
            cwd=REPO, text=True, timeout=15)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def compose_write(rel: str, text: str) -> None:
    dest = f"/data/{rel}"
    parent = str(Path(dest).parent)
    subprocess.run(
        ["docker", "compose", "exec", "-T", "app",
         "mkdir", "-p", parent],
        cwd=REPO, check=False, timeout=15)
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "app",
         "tee", dest],
        cwd=REPO, input=text, text=True, check=False, timeout=15,
        stdout=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"cannot write {dest} (exit {proc.returncode})")


def compose_rm(rel: str) -> None:
    dest = f"/data/{rel}"
    subprocess.run(
        ["docker", "compose", "exec", "-T", "app", "rm", "-f", dest],
        cwd=REPO, check=False, timeout=15)


def docker_name_list(argv: list[str]) -> list[str]:
    try:
        out = subprocess.check_output(argv, text=True, timeout=20)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def apply_prune(*, work: Path, tag: str, house: dict[str, str],
                status: dict) -> dict:
    req = compose_read(PRUNE_REQUEST)
    if req is None:
        return status
    keep_images: list[str] = [f"devcake/dev-hello:{tag}"]
    try:
        ks = load_keep_set(work / KEEP_SET)
    except Exception:  # noqa: BLE001 — prune still runs hello-only if keep-set is junk
        ks = None
    if ks is not None:
        for pin in ks.pins:
            keep_images.append(image_ref(
                pin.template, pin.cli_version, tag=tag, house=house))
    try:
        gone = plan_prune(
            keep_images=keep_images,
            running_images=docker_name_list(
                ["docker", "ps", "-a", "--format", "{{.Image}}"]),
            local_images=docker_name_list(
                ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"]),
        )
        run_prune(gone, run=subprocess.run)
        status = {**status, "prune": {
            "removed": list(gone),
            "kept": len(keep_images),
            "detail": "" if gone else "nothing to prune",
        }}
    except Exception as exc:  # noqa: BLE001 — prune failure is operator-visible, not a baker crash
        status = {**status, "prune": {
            "removed": [], "kept": 0, "detail": str(exc),
        }}
    try:
        compose_rm(PRUNE_REQUEST)
    except Exception:  # noqa: BLE001 — next tick will see a stale request; status already records the result
        pass
    local = work / PRUNE_REQUEST
    if local.is_file():
        local.unlink()
    return status


def compose_append(rel: str, text: str) -> None:
    dest = f"/data/{rel}"
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "app", "tee", "-a", dest],
        cwd=REPO, input=text, text=True, check=False, timeout=15,
        stdout=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"cannot append {dest} (exit {proc.returncode})")


def probe_app_live() -> bool:
    """Same check as up.sh _app_live — the existing health chokepoint."""
    try:
        proc = subprocess.run(
            ["docker", "compose", "exec", "-T", "app", "python", "-c",
             "import urllib.request as u; "
             "u.urlopen('http://localhost:8000/api/v1/health/live', timeout=3)"],
            cwd=REPO, timeout=10, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _checkout_digest() -> str:
    """Bytes of this checkout — the identity receipts must carry."""
    import app_digest
    return app_digest.compute(REPO)


def running_app_digest() -> str | None:
    try:
        out = subprocess.check_output(
            ["docker", "compose", "exec", "-T", "app",
             "printenv", "DEVCAKE_APP_DIGEST"],
            cwd=REPO, text=True, timeout=15).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return out or None


def _dockerfile_text() -> str:
    for candidate in (REPO / "images" / "Dockerfile",
                      Path("/srv/images.Dockerfile")):
        if candidate.is_file():
            return candidate.read_text()
    raise FileNotFoundError("images/Dockerfile missing")


def emit_event(work: Path, record: dict) -> dict:
    rec = append_baker_event(work / BAKER_LOG, record)
    line = json.dumps(rec, separators=(",", ":")) + "\n"
    try:
        compose_append(BAKER_LOG, line)
    except RuntimeError:
        pass
    return rec


def publish_status(work: Path, payload: dict) -> dict:
    body = write_status(work / STATUS, payload)
    try:
        compose_write(STATUS, json.dumps(body, indent=2) + "\n")
    except RuntimeError:
        pass
    return body


def ship_dying_words(record: dict) -> None:
    """Best-effort POST to OO from the host — the app is down, so poll cannot ship."""
    import base64
    import urllib.error
    import urllib.request

    url = os.environ.get("DEVCAKE_OO_URL", "http://127.0.0.1:5080")
    org = os.environ.get("OO_ORG", "default")
    email = os.environ.get("OO_INGEST_EMAIL", "")
    password = os.environ.get("OO_INGEST_PASSWORD", "")
    if not email or not password:
        return
    token = base64.b64encode(f"{email}:{password}".encode()).decode()
    req = urllib.request.Request(
        f"{url}/api/{org}/baker/_json",
        data=json.dumps([record]).encode(),
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except (OSError, urllib.error.URLError):
        pass


def beating_run(work: Path):
    """subprocess.run-shaped: heartbeat while waiting, tee output, keep a tail."""

    def stamp() -> None:
        body = touch_status(work / STATUS)
        try:
            compose_write(STATUS, json.dumps(body, indent=2) + "\n")
        except RuntimeError:
            pass

    def run(argv, **kw):
        return tee_run(
            argv, cwd=kw.get("cwd"), env=kw.get("env"),
            stamp=stamp, interval=INTERVAL)

    return run


def trees_mtime(root: Path) -> float:
    """Newest mtime under the digest trees. 0 if none exist."""
    import app_digest
    latest = 0.0
    for rel in app_digest.TREES:
        p = Path(root) / rel
        if p.is_file():
            latest = max(latest, p.stat().st_mtime)
        elif p.is_dir():
            for q in p.rglob("*"):
                if q.is_file() and "__pycache__" not in q.parts:
                    latest = max(latest, q.stat().st_mtime)
    return latest


_IDLE = frozenset({"ready", "virgin"})


def skip_reconcile(*, state: str | None, trees: float | None, keep: float | None,
                   last_trees: float | None, last_keep: float | None) -> bool:
    """Unknown mtimes (None) force a full tick — never compare 0==0."""
    if state not in _IDLE:
        return False
    if None in (trees, keep, last_trees, last_keep):
        return False
    return trees == last_trees and keep == last_keep


def keep_set_mtime() -> float | None:
    try:
        out = subprocess.check_output(
            ["docker", "compose", "exec", "-T", "app",
             "stat", "-c", "%Y", f"/data/{KEEP_SET}"],
            cwd=REPO, text=True, timeout=15)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    try:
        return float(out.strip())
    except ValueError:
        return None


def once(*, work: Path, tag: str, house: dict[str, str],
         digest: str) -> dict:
    keep_path = work / KEEP_SET
    text = compose_read(KEEP_SET)
    if text is None:
        if keep_path.exists():
            keep_path.unlink()
    else:
        keep_path.write_text(text)
    receipts = work / RECEIPTS
    receipts.mkdir(parents=True, exist_ok=True)
    for name in compose_ls(RECEIPTS):
        if not name.endswith(".json"):
            continue
        body = compose_read(f"{RECEIPTS}/{name}")
        if body is not None:
            (receipts / name).write_text(body)

    def baker(job):
        try:
            run_bake(
                job, tag=tag, house=house, receipts_dir=receipts,
                digest=digest, repo=REPO, run=beating_run(work))
        finally:
            # A receipt is the bake verb's result even when the gate failed.
            local = receipt_path(receipts, job)
            if local.is_file():
                compose_write(f"{RECEIPTS}/{local.name}", local.read_text())

    status = reconcile(
        keep_set_path=keep_path,
        receipts_dir=receipts,
        status_path=work / STATUS,
        digest=digest,
        baker=baker,
        tag=tag,
        house=house,
    )
    status = apply_prune(work=work, tag=tag, house=house, status=status)
    try:
        compose_write(STATUS, json.dumps(status, indent=2) + "\n")
    except RuntimeError as exc:
        status = {**status, "state": "error",
                  "detail": f"{status.get('detail', '')} ({exc})".strip()}
    return status


def main(argv: list[str] | None = None) -> int:
    del argv  # reserved
    tag = os.environ.get("DEVCAKE_TAG", "latest")
    house = house_from_dockerfile(_dockerfile_text())
    work = Path(os.environ.get(
        "DEVCAKE_FACTORY_WORK", str(REPO / ".factory" / "work")))
    work.mkdir(parents=True, exist_ok=True)
    print(f"dev_factory: watching keep-set every {INTERVAL:.0f}s "
          f"(tag={tag})", flush=True)
    down_streak = 0
    last_state: str | None = None
    last_trees: float | None = None
    last_keep: float | None = None
    cached_digest: str | None = None
    cached_trees: float | None = None
    while True:
        healthy = probe_app_live()
        if not healthy:
            down_streak += 1
            print(f"dev_factory: app /health/live failed "
                  f"({down_streak}/{UNHEALTHY_NEED})", flush=True)
            if unhealthy_verdict(down_streak):
                rec = emit_event(work, {
                    "event": "down",
                    "detail": "app /health/live failed — baker exiting",
                })
                ship_dying_words(rec)
                print("dev_factory: app is not healthy — exiting "
                      "(restart with ./up.sh)", flush=True)
                return 1
            time.sleep(INTERVAL)
            continue
        down_streak = 0
        trees = trees_mtime(REPO)
        keep_m = keep_set_mtime()
        prune_pending = compose_read(PRUNE_REQUEST) is not None
        if (not prune_pending and skip_reconcile(
                state=last_state, trees=trees, keep=keep_m,
                last_trees=last_trees, last_keep=last_keep)):
            path = work / STATUS
            current = {}
            if path.is_file():
                try:
                    current = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    current = {}
            publish_status(work, current or {"state": "ready", "jobs": []})
            # Wake often enough to see a prune request; skip_reconcile already
            # avoided the digest/bake work.
            time.sleep(INTERVAL)
            continue
        if cached_digest is None or cached_trees != trees:
            cached_digest = _checkout_digest()
            cached_trees = trees
        checkout = cached_digest
        digest = running_app_digest()
        kind = classify_app(
            healthy=True, digest=digest, checkout=checkout)
        action = tick_decision(kind)
        if action == "heartbeat":
            if kind == "mismatch":
                detail = (
                    "the checkout has moved since the app was baked; "
                    "run ./up.sh --bake")
            else:
                detail = "this app was built without the bake wrapper"
            publish_status(work, {
                "state": "error",
                "digest": checkout or digest or SENTINEL,
                "jobs": [],
                "detail": detail,
            })
            emit_event(work, {"event": kind, "detail": detail})
            print(f"dev_factory: {detail}", flush=True)
            last_state = "error"
            last_trees = trees
            last_keep = keep_m
            time.sleep(INTERVAL)
            continue
        try:
            status = once(
                work=work, tag=tag, house=house,
                digest=checkout)
        except Exception as exc:  # noqa: BLE001 — one failed tick must not kill a healthy app
            print(f"dev_factory: tick failed: {exc}", flush=True)
            publish_status(work, {
                "state": "error", "digest": digest or "",
                "jobs": [], "detail": str(exc),
            })
            emit_event(work, {"event": "error", "detail": str(exc)})
            last_state = "error"
        else:
            publish_status(work, status)
            emit_event(work, {
                "event": "tick",
                "state": status.get("state"),
                "jobs": len(status.get("jobs") or []),
            })
            print(f"dev_factory: {status.get('state')} "
                  f"jobs={len(status.get('jobs') or [])}", flush=True)
            last_state = status.get("state")
        last_trees = trees
        last_keep = keep_m
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
