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

from .core import (
    house_from_dockerfile,
    reconcile,
    run_bake,
    write_status,
)
from .liveness import (
    SENTINEL,
    append_baker_event,
    classify_app,
    tick_decision,
)

REPO = Path(__file__).resolve().parents[2]
INTERVAL = float(os.environ.get("DEVCAKE_FACTORY_INTERVAL", "5"))
KEEP_SET = "harness_keep_set.json"
STATUS = "harness_bake_status.json"
RECEIPTS = "harness_receipts"
BAKER_LOG = "harness_baker.jsonl"


def data_volume_name() -> str | None:
    """Named volume mounted at app:/data. Empty if the stack is not up."""
    try:
        cid = subprocess.check_output(
            ["docker", "compose", "ps", "-q", "app"],
            cwd=REPO, text=True, timeout=15).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    if not cid:
        return None
    try:
        mounts = subprocess.check_output(
            ["docker", "inspect", "-f",
             "{{ range .Mounts }}{{ if eq .Destination \"/data\" }}"
             "{{ .Name }}{{ end }}{{ end }}", cid],
            text=True, timeout=15).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return mounts or None


def compose_read(rel: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["docker", "compose", "exec", "-T", "app", "cat", f"/data/{rel}"],
            cwd=REPO, text=True, timeout=15)
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
    """subprocess.run-shaped, but stamps a heartbeat while docker bake waits."""

    def run(argv, **kw):
        proc = subprocess.Popen(
            argv, cwd=kw.get("cwd"), env=kw.get("env"),
            stdout=kw.get("stdout"), stderr=kw.get("stderr"))
        while proc.poll() is None:
            current = {}
            path = work / STATUS
            if path.is_file():
                try:
                    current = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    current = {}
            publish_status(work, current or {"state": "baking", "jobs": []})
            time.sleep(INTERVAL)
        return type("R", (), {"returncode": proc.returncode})()

    return run


def once(*, work: Path, tag: str, house: dict[str, str],
         digest: str, volume: str | None) -> dict:
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
        run_bake(
            job, tag=tag, house=house, receipts_dir=receipts,
            digest=digest, repo=REPO, run=beating_run(work),
            receipts_volume=volume)
        name = f"{job.template}@{job.cli_version}.json"
        local = receipts / name
        remote = compose_read(f"{RECEIPTS}/{name}")
        if remote is not None:
            local.write_text(remote)
        elif local.is_file():
            compose_write(f"{RECEIPTS}/{name}", local.read_text())

    status = reconcile(
        keep_set_path=keep_path,
        receipts_dir=receipts,
        status_path=work / STATUS,
        digest=digest,
        baker=baker,
        tag=tag,
        house=house,
    )
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
    while True:
        healthy = probe_app_live()
        digest = running_app_digest() if healthy else None
        kind = classify_app(healthy=healthy, digest=digest)
        action = tick_decision(kind)
        if action == "exit":
            rec = emit_event(work, {
                "event": "down",
                "detail": "app /health/live failed — baker exiting",
            })
            ship_dying_words(rec)
            print("dev_factory: app is not healthy — exiting "
                  "(restart with ./up.sh)", flush=True)
            return 1
        if action == "heartbeat":
            publish_status(work, {
                "state": "error",
                "digest": digest or SENTINEL,
                "jobs": [],
                "detail": "this app was built without the bake wrapper",
            })
            emit_event(work, {"event": "sentinel",
                              "detail": "app digest is the sentinel"})
            print("dev_factory: app digest is sentinel — heartbeat only "
                  "(rebuild app via ./up.sh --bake)", flush=True)
            time.sleep(INTERVAL)
            continue
        volume = data_volume_name()
        try:
            status = once(
                work=work, tag=tag, house=house,
                digest=digest or "", volume=volume)
        except Exception as exc:  # noqa: BLE001 — one failed tick must not kill a healthy app
            print(f"dev_factory: tick failed: {exc}", flush=True)
            publish_status(work, {
                "state": "error", "digest": digest or "",
                "jobs": [], "detail": str(exc),
            })
            emit_event(work, {"event": "error", "detail": str(exc)})
        else:
            publish_status(work, status)
            emit_event(work, {
                "event": "tick",
                "state": status.get("state"),
                "jobs": len(status.get("jobs") or []),
            })
            print(f"dev_factory: {status.get('state')} "
                  f"jobs={len(status.get('jobs') or [])}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
