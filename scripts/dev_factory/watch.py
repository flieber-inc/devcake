"""Host baker loop. Started by devcake up — not a compose service.

Reads the keep-set the app published into the /data volume, validates it
independently, compiles + probes, writes receipts and a status file the
app surfaces as baking / ready. The app never talks to Docker.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

REPO = Path(__file__).resolve().parents[2]

from .spans import new_ids, probe_spans_from_receipt, span_record
from .core import (
    TAKING_SUFFIX,
    InvalidKeepSet,
    claim_inbox,
    drop_receipts_missing_images,
    house_from_dockerfile,
    pins_moved_with_tag,
    attach_newest_nested,
    carry_last_prune,
    nested_probe_due,
    newest_nested_receipt,
    probe_image_candidates,
    resolve_apparmor_profile,
    prune_outcome,
    receipts_to_push,
    image_ref,
    load_keep_set,
    plan_prune,
    prune_keep_list,
    receipt_path,
    reconcile,
    release_inbox,
    run_bake,
    run_prune,
    touch_status,
    write_status,
)
from .run import tee_run
from .liveness import (
    SENTINEL,
    UNHEALTHY_BUDGET_S,
    classify_app,
    tick_decision,
    unhealthy_backoff_s,
    unhealthy_verdict,
)
INTERVAL = float(os.environ.get("DEVCAKE_FACTORY_INTERVAL", "5"))
KEEP_SET = "harness_keep_set.json"
STATUS = "harness_bake_status.json"
RECEIPTS = "harness_receipts"
BAKER_LOG = "harness_baker.jsonl"
OUTBOX = "harness_outbox"
PRUNE_REQUEST = "harness_prune_request.json"
# Host redirect target (devcake up / systemd / launchd / respawn). Cap keeps idle
# noise from filling disk.
WATCH_LOG_CAP_BYTES = 2 * 1024 * 1024  # 2 MiB
# Exclusive lock + pidfile so any supervisor combo cannot double-run the baker.
BAKER_LOCK_NAME = "watch.lock"
BAKER_PID_NAME = "watch.pid"


def acquire_baker_singleton(factory_dir: Path | str) -> IO[str]:
    """Take an exclusive flock on watch.lock and write watch.pid.

    Holds the lock for the process lifetime (caller must keep the returned
    file open). On contention exits 0 so Restart=on-failure / launchd
    KeepAlive (SuccessfulExit=false) do not restart-storm a healthy peer.
    """
    dest = Path(factory_dir)
    dest.mkdir(parents=True, exist_ok=True)
    lock_path = dest / BAKER_LOCK_NAME
    pid_path = dest / BAKER_PID_NAME
    fh = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115 — held open for flock lifetime
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        holder = "unknown"
        try:
            text = lock_path.read_text(encoding="utf-8").strip()
            if text:
                holder = text
        except OSError:
            pass
        print(
            "dev_factory: another baker already holds "
            f"{lock_path} (pid {holder}) — exiting without stealing the lock",
            flush=True,
        )
        raise SystemExit(0) from None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    pid_path.write_text(f"{os.getpid()}\n")
    return fh


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


def compose_claim(rel: str) -> None:
    """Rename /data/rel → /data/rel.taking if the inbox is present.

    Paths ride argv ($1/$2), never shell interpolation — keep-set / prune
    names are fixed constants today; this stays safe if that changes.
    """
    src = f"/data/{rel}"
    dst = f"/data/{rel}{TAKING_SUFFIX}"
    subprocess.run(
        ["docker", "compose", "exec", "-T", "app",
         "sh", "-c", 'if [ -f "$1" ]; then mv -f "$1" "$2"; fi',
         "claim", src, dst],
        cwd=REPO, check=False, timeout=15)


def docker_name_list(argv: list[str]) -> list[str] | None:
    """None = the listing failed (do not treat as zero images)."""
    try:
        out = subprocess.check_output(argv, text=True, timeout=20)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def apply_prune(*, work: Path, tag: str, house: dict[str, str],
                status: dict, keep_set=None,
                trace_id: str = "", parent: str = "") -> dict:
    compose_claim(PRUNE_REQUEST)
    taking = PRUNE_REQUEST + TAKING_SUFFIX
    text = compose_read(taking)
    prune_path = work / PRUNE_REQUEST
    if prune_path.exists():
        prune_path.unlink()
    if text is not None:
        prune_path.write_text(text)
    claimed = claim_inbox(prune_path)
    if claimed is None:
        return status
    prune_t0 = time.time_ns()
    keep_images = prune_keep_list(keep_set, tag=tag, house=house)
    if keep_images is None:
        release_inbox(claimed)
        compose_rm(taking)
        compose_rm(PRUNE_REQUEST)
        print("dev_factory: prune refused: no keep-set order this tick",
              flush=True)
        return {**status, "prune": prune_outcome(
            removed=[], kept=0,
            detail="refused: no keep-set order this tick")}
    try:
        running = docker_name_list(
            ["docker", "ps", "-a", "--format", "{{.Image}}"])
        local = docker_name_list(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"])
        if running is None or local is None:
            raise RuntimeError("docker listing failed — prune refused")
        gone = plan_prune(
            keep_images=keep_images,
            running_images=running,
            local_images=local,
        )
        run_prune(gone, run=subprocess.run)
        local = docker_name_list(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"])
        dropped = drop_receipts_missing_images(
            work / RECEIPTS, local_images=local, tag=tag, house=house)
        for stem in dropped:
            try:
                compose_rm(f"{RECEIPTS}/{stem}.json")
            except Exception:  # noqa: BLE001 — projection rewrite is best-effort
                pass
        status = {**status, "prune": prune_outcome(
            removed=gone, kept=len(keep_images),
            detail="" if gone else "nothing to prune",
            receipts_dropped=dropped)}
        print(("dev_factory: prune: removed %d image(s): %s"
               % (len(gone), ", ".join(gone))) if gone else
              "dev_factory: prune: nothing to prune (kept %d)" % len(keep_images),
              flush=True)
    except Exception as exc:  # noqa: BLE001 — prune failure is operator-visible, not a baker crash
        status = {**status, "prune": prune_outcome(
            removed=[], kept=0, detail=str(exc))}
        print(f"dev_factory: prune failed: {exc}", flush=True)
    release_inbox(claimed)
    compose_rm(taking)
    compose_rm(PRUNE_REQUEST)
    tid, parent_id = trace_id, parent
    if not tid:
        tid, parent_id = new_ids()
        parent_id = ""
    _, prune_sid = new_ids()
    err = (status.get("prune") or {}).get("detail")
    emit_event(work, span_record(
        name="baker.prune",
        trace_id=tid,
        span_id=prune_sid,
        parent=parent_id,
        start_ns=prune_t0,
        end_ns=time.time_ns(),
        status="error" if err else "ok",
        removed=len((status.get("prune") or {}).get("removed") or []),
    ))
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
    """Same check as devcake up _app_live — the existing health chokepoint."""
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
    rec = dict(record)
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
    box = work / OUTBOX
    box.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{rec.get('event', 'evt')}.jsonl"
    dest = box / name
    dest.write_text(json.dumps(rec, separators=(",", ":")) + "\n")
    try:
        compose_write(f"{OUTBOX}/{name}", dest.read_text())
    except RuntimeError:
        pass
    return rec


def _read_local_status(work: Path) -> dict:
    try:
        body = json.loads((work / STATUS).read_text())
        return body if isinstance(body, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def publish_status(work: Path, payload: dict) -> dict:
    # the last prune's outcome rides every status the baker publishes until
    # the next prune (core.carry_last_prune)
    payload = carry_last_prune(_read_local_status(work), payload)
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
    """No inbox (keep is None) is idle — nothing to honor. Unknown *tree*
    mtimes still force a tick. Never compare 0==0."""
    if state not in _IDLE:
        return False
    if keep is None:
        return True
    if None in (trees, last_trees, last_keep):
        return False
    return trees == last_trees and keep == last_keep


def keep_set_mtime() -> float | None:
    try:
        out = subprocess.check_output(
            ["docker", "compose", "exec", "-T", "app",
             "stat", "-c", "%Y", f"/data/{KEEP_SET}"],
            cwd=REPO, text=True, timeout=15,
            stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    try:
        return float(out.strip())
    except ValueError:
        return None


def rotate_watch_log(path: Path | str, *,
                     cap: int = WATCH_LOG_CAP_BYTES) -> bool:
    """Copytruncate when the live log exceeds *cap*.

    Returns True when rotation happened. Truncates the same inode so an
    open stdout/stderr redirect (nohup or systemd append) keeps writing.
    """
    dest = Path(path)
    try:
        size = dest.stat().st_size
    except OSError:
        return False
    if size <= cap:
        return False
    bak = dest.with_name(dest.name + ".1")
    try:
        import shutil
        shutil.copy2(dest, bak)
        with dest.open("r+b") as fh:
            fh.truncate(0)
    except OSError:
        return False
    return True


def once(*, work: Path, tag: str, house: dict[str, str],
         digest: str) -> dict:
    previous = _read_local_status(work)     # the last prune outcome, if any
    compose_claim(KEEP_SET)
    taking_name = KEEP_SET + TAKING_SUFFIX
    text = compose_read(taking_name)
    keep_path = work / KEEP_SET
    if keep_path.exists():
        keep_path.unlink()
    if text is not None:
        keep_path.write_text(text)
    claimed = claim_inbox(keep_path)
    receipts = work / RECEIPTS
    receipts.mkdir(parents=True, exist_ok=True)
    present = set(compose_ls(RECEIPTS))
    for name in present:
        if not name.endswith(".json"):
            continue
        body = compose_read(f"{RECEIPTS}/{name}")
        if body is not None:
            (receipts / name).write_text(body)

    images = docker_name_list(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"])
    dropped = drop_receipts_missing_images(
        receipts, local_images=images, tag=tag, house=house)
    listed = list(images) if images is not None else []
    for stem in dropped:
        try:
            compose_rm(f"{RECEIPTS}/{stem}.json")
        except Exception:  # noqa: BLE001 — gone-image receipt must not block the tick
            pass
    # the container copy is a projection: re-push any local receipt for
    # this digest the container lacks (a write that failed while the app
    # was being recreated lands on a later tick — the bake itself was fine)
    for path in receipts_to_push(receipts, present=present, digest=digest):
        try:
            compose_write(f"{RECEIPTS}/{path.name}", path.read_text())
            print(f"dev_factory: receipt pushed to the app: {path.name}", flush=True)
        except (OSError, RuntimeError) as exc:
            print(f"dev_factory: receipt {path.name} not yet in the app "
                  f"({exc}) — retrying next tick", flush=True)
    # A tag move is a bake order: a dropped receipt whose pin still has an
    # image under another tag was wanted, and nothing may be left to say so
    # (the app's boot-time order can be claimed by the outgoing baker during
    # a deploy). Only when no order is in the inbox — a real order wins.
    synthesized: Path | None = None
    if claimed is None:
        moved = pins_moved_with_tag(
            dropped, local_images=images, tag=tag, house=house)
        if moved:
            synthesized = work / (KEEP_SET + ".rebake")
            synthesized.write_text(json.dumps({"pins": [
                {"template": p.template, "cli_version": p.cli_version}
                for p in moved]}, indent=2) + "\n")
            claimed = synthesized
            print("dev_factory: tag moved to "
                  f"{tag} — rebaking {len(moved)} receipted pin(s): "
                  + ", ".join(f"{p.template}@{p.cli_version}" for p in moved),
                  flush=True)

    keep_set = None
    trace_id, root_id = "", ""
    baked_images: list[str] = []      # harness images baked green this tick
    t0 = time.time_ns()
    if claimed is None:
        # Hello is baked by every devcake up — not evidence of staffing
        # (matches core.reconcile virgin semantics).
        has_dev = any(
            str(ref).startswith("devcake/dev-")
            and not str(ref).startswith("devcake/dev-hello:")
            and not str(ref).startswith("devcake/dev-hello<")
            for ref in listed
        )
        status = write_status(work / STATUS, {
            "state": "ready" if has_dev else "virgin",
            "digest": digest,
            "jobs": [],
            "detail": "" if has_dev else "no keep-set — control plane + hello only",
        })
    else:
        try:
            keep_set = load_keep_set(claimed)
        except InvalidKeepSet:
            keep_set = None
        trace_id, root_id = new_ids()

        def baker(job):
            c0 = time.time_ns()
            _, compile_sid = new_ids()
            rec: dict = {}
            try:
                run_bake(
                    job, tag=tag, house=house, receipts_dir=receipts,
                    digest=digest, repo=REPO, run=beating_run(work))
                baked_images.append(
                    image_ref(job.template, job.cli_version, tag=tag, house=house))
            finally:
                local = receipt_path(receipts, job)
                if local.is_file():
                    try:
                        compose_write(f"{RECEIPTS}/{local.name}", local.read_text())
                    except (OSError, RuntimeError) as exc:
                        # the bake and probe are done and recorded locally;
                        # the container copy is pushed on the next tick
                        print(f"dev_factory: receipt {local.name} written locally; "
                              f"the app copy waits for the next tick ({exc})",
                              flush=True)
                    try:
                        loaded = json.loads(local.read_text())
                    except (OSError, json.JSONDecodeError):
                        loaded = {}
                    if isinstance(loaded, dict):
                        rec = loaded
                c1 = time.time_ns()
                emit_event(work, span_record(
                    name="baker.compile",
                    trace_id=trace_id, span_id=compile_sid, parent=root_id,
                    start_ns=c0, end_ns=c1,
                    status="error" if not rec.get("ok") else "ok",
                    template=job.template, cli_version=job.cli_version))
                for kid in probe_spans_from_receipt(
                        rec, trace_id=trace_id, parent=compile_sid,
                        start_ns=c0, end_ns=c1):
                    emit_event(work, kid)

        status = reconcile(
            keep_set_path=claimed,
            receipts_dir=receipts,
            status_path=work / STATUS,
            digest=digest,
            baker=baker,
            tag=tag,
            house=house,
            local_images=images,
        )
        release_inbox(claimed)
        # Only the claimed generation (`.taking`) may be removed. A file at
        # KEEP_SET after claim is a newer app publication for the next tick —
        # never delete the live publication path. A synthesized order never
        # existed in the container.
        if synthesized is None:
            compose_rm(taking_name)
        emit_event(work, span_record(
            name="baker.reconcile",
            trace_id=trace_id, span_id=root_id, parent="",
            start_ns=t0, end_ns=time.time_ns(),
            status="error" if status.get("state") == "error" else "ok",
        ))
    status = apply_prune(
        work=work, tag=tag, house=house, status=status, keep_set=keep_set,
        trace_id=trace_id, parent=root_id)
    status = carry_last_prune(previous, status)
    status = publish_nested(
        work=work, previous=previous, status=status, baked=baked_images,
        local_images=listed, tag=tag, trace_id=trace_id, parent=root_id)
    write_status(work / STATUS, status)     # the local copy is what the
    #                                         heartbeat and the next tick read
    try:
        compose_write(STATUS, json.dumps(status, indent=2) + "\n")
    except RuntimeError as exc:
        status = {**status, "state": "error",
                  "detail": f"{status.get('detail', '')} ({exc})".strip()}
    return status


_APPLY_CACHE: dict = {"at": 0.0, "profile": "", "value": None}
APPLY_CHECK_INTERVAL_S = 60.0


def profile_still_applies(now: float | None = None) -> bool | None:
    """Ask the daemon, at most once a minute, whether it still applies the
    profile .env names — the one drift the receipt cannot see (the profile
    removed by hand or lost at boot leaves .env, the receipt and the DAG
    agreeing while every run dies at create). None = not applicable
    (docker-default) or unknown (no image to try, daemon unreachable)."""
    profile = resolve_apparmor_profile(REPO, os.environ)
    if profile == "docker-default":
        return None
    t = time.time() if now is None else now
    if (_APPLY_CACHE["profile"] == profile
            and t - _APPLY_CACHE["at"] < APPLY_CHECK_INTERVAL_S):
        return _APPLY_CACHE["value"]
    value: bool | None = None
    try:
        images = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True, text=True, timeout=30, check=False).stdout
        image = next((r for r in images.splitlines()
                      if r.startswith("devcake/dev-hello:") and not r.endswith(":<none>")),
                     None)
        if image:
            # the same throwaway the doctor runs (cli/devcake_cli/doctor.py);
            # here a failure the daemon does not attribute to AppArmor stays
            # "unknown" — a transient hiccup must not flip every surface red
            # for a minute, the doctor is where an operator gets the full line
            proc = subprocess.run(
                ["docker", "run", "--rm", "--network", "none", "--user", "65534:65534",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
                 "--security-opt", f"apparmor={profile}", "--entrypoint", "/bin/true",
                 image], capture_output=True, text=True, timeout=90, check=False)
            if proc.returncode == 0:
                value = True
            elif "apparmor" in (proc.stderr or "").lower():
                value = False
    except (OSError, subprocess.TimeoutExpired):
        value = None
    _APPLY_CACHE.update(at=t, profile=profile, value=value)
    return value


def _installed_profile_sha256() -> str:
    """sha256 of /etc/apparmor.d/devcake-nested, empty when absent."""
    import hashlib
    try:
        return hashlib.sha256(Path("/etc/apparmor.d/devcake-nested").read_bytes()).hexdigest()
    except OSError:
        return ""


def _host_facts() -> tuple[str, str]:
    """(kernel, engine) as the probe records them; empty when unreadable."""
    out = []
    for fmt in ("{{.KernelVersion}}", "{{.ServerVersion}}"):
        try:
            proc = subprocess.run(["docker", "info", "--format", fmt],
                                  capture_output=True, text=True, timeout=30, check=False)
            out.append((proc.stdout or "").strip() if proc.returncode == 0 else "")
        except (OSError, subprocess.TimeoutExpired):
            out.append("")
    return out[0], out[1]


_PROBE_BACKOFF: dict = {"until": 0.0}
PROBE_RETRY_BACKOFF_S = 600.0

_DRIFT_CACHE: dict = {"at": 0.0, "value": False}
DRIFT_CHECK_INTERVAL_S = 60.0


def nested_drift_pending(now: float | None = None) -> bool:
    """Once a minute on idle ticks: does the newest receipt still describe
    the contract the stack runs (profile, seccomp blob, kernel, engine)?
    True forces a full tick, whose publish_nested re-probes — so an engine
    upgrade without a reboot re-measures without waiting for a bake or a
    restart. Never re-probes on its own."""
    t = time.time() if now is None else now
    if t - _DRIFT_CACHE["at"] < DRIFT_CHECK_INTERVAL_S:
        return bool(_DRIFT_CACHE["value"])
    try:
        receipt = newest_nested_receipt(REPO / ".factory")
        kernel, engine = _host_facts()
        value = nested_probe_due(
            baked_now=False, receipt=receipt,
            apparmor_profile=resolve_apparmor_profile(REPO, os.environ),
            seccomp_sha256=_dag_seccomp_sha256(), kernel=kernel, engine=engine,
            profile_sha256=_installed_profile_sha256())
        if value and t < _PROBE_BACKOFF["until"]:
            value = False
    except Exception:  # noqa: BLE001 — a drift check must never kill the loop
        value = False
    _DRIFT_CACHE.update(at=t, value=value)
    return value


def _dag_seccomp_sha256() -> str:
    """The sha of the seccomp blob the DAG ships now — the probe records
    the one it sent, so a DAG change makes the last verdict stale."""
    import hashlib
    try:
        out = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "harness_probe" / "nested_seccomp.py"),
             str(REPO / "dagu" / "dags" / "dev-run.yaml")],
            capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if out.returncode != 0:
        return ""
    return hashlib.sha256((out.stdout.rstrip("\n") + "\n").encode()).hexdigest()


def publish_nested(*, work: Path, previous, status: dict, baked: list[str],
                   local_images, tag: str, trace_id: str = "",
                   parent: str = "") -> dict:
    """The nested-engine receipt in the bake status. After a green harness
    bake — or when the newest receipt no longer describes the contract
    the stack runs — the probe replays the dev-run contract against a
    harness image; the newest receipt (hand-run ones included) is then
    published as `nested`. Never fatal, never gating: a red rig degrades
    Devs and says so on every surface, it does not unstaff them. The
    profile name comes from the checkout's .env — what compose handed the
    DAG — never from this process's environment alone (a supervised baker
    has none of it)."""
    factory = REPO / ".factory"
    receipt = newest_nested_receipt(factory)
    profile = resolve_apparmor_profile(REPO, os.environ)
    kernel, engine = _host_facts()
    due = nested_probe_due(baked_now=bool(baked), receipt=receipt,
                           apparmor_profile=profile,
                           seccomp_sha256=_dag_seccomp_sha256(),
                           kernel=kernel, engine=engine,
                           profile_sha256=_installed_profile_sha256())
    candidates = probe_image_candidates(local_images, tag=tag)
    image = baked[-1] if baked else (candidates[0] if candidates else "")
    # a probe that produced no receipt (script error, timeout) must not
    # re-run every tick: one retry every ten minutes until it does
    if due and not baked and time.time() < _PROBE_BACKOFF["until"]:
        due = False
    if due and not image:
        # nothing to probe with (no harness image baked yet, or all pruned):
        # the drift stands until a bake; do not force a tick every minute
        _PROBE_BACKOFF["until"] = time.time() + PROBE_RETRY_BACKOFF_S
    if due and image:
        before = (receipt or {}).get("measured_at")
        p0 = time.time_ns()
        _, sid = new_ids()
        print(f"dev_factory: nested-engine probe on {image} "
              f"(apparmor={profile})", flush=True)
        try:
            result = beating_run(work)(
                ["bash", str(REPO / "scripts" / "harness_probe" / "nested_probe.sh"), image],
                cwd=str(REPO), env={**os.environ, "DEVCAKE_APPARMOR_PROFILE": profile})
            rc = int(getattr(result, "returncode", 1))
        except Exception as exc:  # noqa: BLE001 — the probe must never wedge the tick
            print(f"dev_factory: nested-engine probe failed to run ({exc})", flush=True)
            rc = 1
        receipt = newest_nested_receipt(factory)
        if (receipt or {}).get("measured_at") == before:
            _PROBE_BACKOFF["until"] = time.time() + PROBE_RETRY_BACKOFF_S
            print("dev_factory: the nested-engine probe wrote no receipt — "
                  "next try in 10 minutes", flush=True)
        emit_event(work, span_record(
            name="baker.nested_probe", trace_id=trace_id or new_ids()[0],
            span_id=sid, parent=parent, start_ns=p0, end_ns=time.time_ns(),
            status="ok" if rc == 0 else "error", image=image,
            apparmor_profile=profile,
            first_red=str((receipt or {}).get("first_red") or "")))
    return attach_newest_nested(status, factory, previous,
                                profile_applies=profile_still_applies())


def _watch_log_path() -> Path:
    return Path(os.environ.get(
        "DEVCAKE_FACTORY_LOG", str(REPO / ".factory" / "watch.log")))


def main(argv: list[str] | None = None) -> int:
    del argv  # reserved
    tag = os.environ.get("DEVCAKE_TAG", "latest")
    house = house_from_dockerfile(_dockerfile_text())
    work = Path(os.environ.get(
        "DEVCAKE_FACTORY_WORK", str(REPO / ".factory" / "work")))
    work.mkdir(parents=True, exist_ok=True)
    # Singleton under any supervisor (systemd / launchd / respawn loop).
    factory_root = work.parent if work.name == "work" else work
    _singleton = acquire_baker_singleton(
        Path(os.environ.get("DEVCAKE_FACTORY_DIR", str(factory_root))))
    watch_log = _watch_log_path()
    rotate_watch_log(watch_log)
    print(f"dev_factory: watching keep-set every {INTERVAL:.0f}s "
          f"(tag={tag})", flush=True)
    down_streak = 0
    down_elapsed = 0.0
    last_state: str | None = None
    last_trees: float | None = None
    last_keep: float | None = None
    cached_digest: str | None = None
    cached_trees: float | None = None
    while True:
        rotate_watch_log(watch_log)
        healthy = probe_app_live()
        if not healthy:
            down_streak += 1
            remaining = UNHEALTHY_BUDGET_S - down_elapsed
            if remaining <= 0 or unhealthy_verdict(elapsed_s=down_elapsed):
                rec = emit_event(work, {
                    "event": "down",
                    "detail": "app /health/live failed — baker exiting",
                })
                ship_dying_words(rec)
                print("dev_factory: app is not healthy — exiting "
                      "(restart with devcake up)", flush=True)
                return 1
            delay = min(unhealthy_backoff_s(down_streak), remaining)
            print(f"dev_factory: app /health/live failed "
                  f"(streak={down_streak}, ~{remaining:.0f}s budget left; "
                  f"backoff {delay:.0f}s)", flush=True)
            time.sleep(delay)
            down_elapsed += delay
            continue
        down_streak = 0
        down_elapsed = 0.0
        trees = trees_mtime(REPO)
        keep_m = keep_set_mtime()
        prune_pending = compose_read(PRUNE_REQUEST) is not None
        if (not prune_pending and not nested_drift_pending() and skip_reconcile(
                state=last_state, trees=trees, keep=keep_m,
                last_trees=last_trees, last_keep=last_keep)):
            path = work / STATUS
            current = {}
            if path.is_file():
                try:
                    current = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    current = {}
            # a hand-run nested probe must show within a tick, idle or not;
            # a profile the daemon no longer applies flips a green receipt
            publish_status(work, attach_newest_nested(
                current or {"state": "ready", "jobs": []}, REPO / ".factory",
                profile_applies=profile_still_applies()))
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
                    "run devcake up --bake")
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
