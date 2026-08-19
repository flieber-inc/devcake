#!/usr/bin/env python3
"""Export a host's receipts pack — the evidence trail — BEFORE it decays.

Two clocks erase receipts on a live host: OpenObserve retention ages spans
out, and Clear-runs sweeps run records and `activity-*` repos. This script
captures, into one private directory (mode ``0o700``; default under
``$XDG_DATA_HOME/devcake/receipts/`` or ``~/.local/share/devcake/receipts/``):

  1. deploy identity — git describe/HEAD, DEVCAKE_TAG, image digests,
     ``docker compose ps`` (which code actually ran);
  2. every TRACES stream in OpenObserve, exported raw as JSONL (the span
     record IS the receipt: token reports, fault classes, durations —
     aggregates stay derivable offline; docs/16 two-ledger doctrine: OO is
     exhaust, so this export is a copy of derived data, never load-bearing);
  3. a sanitized /data tarball — run records, state, audit log — with
     ``/data/secrets`` EXCLUDED (``--exclude ./secrets``). Unlike
     backup_data.sh, this pack is not a secret-tier dump, but it still holds
     **mission content** and must stay private — never “safe to share”
     publicly.

Log streams (container logs) are NOT exported by default — they are bulky
and rarely evidence — but they are NAMED in MANIFEST.json
(``skipped_log_streams``) so the omission is visible (no silent caps). Pass
``--include-logs`` to export them too. Root OO password is used only for the
API call; it is never written into the pack.

This is NOT a backup. Full recovery needs scripts/backup_data.sh +
scripts/backup_gitea.sh (both include secrets — password-export handling).

Usage: scripts/export_receipts.py [--days N] [--out DIR] [--include-logs]
Reads .env for OO_URL / OO_ORG / OO_ROOT_EMAIL / OO_ROOT_PASSWORD.
Run from the repo root on the DevCake host. Fail-loud: any OO error body
is printed verbatim and exits non-zero.
"""

import argparse
import base64
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

PAGE = 10_000
MAX_PAGES = 500          # 5M spans per stream; loud, never silent (below)

# Digest-pinned (2026-08-12 audit OPS-M1): runs as root
# with RW on the output dir — check_image_pins.py enforces the pin.
ALPINE_IMAGE = ("alpine:3.22@sha256:"
                "14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce")


def env(name, default=None):
    for line in open(".env"):
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    if default is not None:
        return default
    sys.exit(f"missing {name} in .env")


def req(method, path, body=None):
    r = urllib.request.Request(f"{OO}/api/{ORG}{path}", method=method,
                               data=json.dumps(body).encode() if body else None,
                               headers={"Authorization": f"Basic {AUTH}",
                                        "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        sys.exit(f"OO {method} {path} → {e.code}: {e.read().decode()[:600]}")
    except urllib.error.URLError as e:
        sys.exit(f"OO unreachable at {OO}: {e}")


def sh(cmd):
    """Best-effort host command for the manifest — records failure, never dies."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=60).stdout.strip()
    except Exception as e:  # noqa: BLE001 — best-effort probe: unavailable host detail must not abort the receipts pack
        return f"<unavailable: {e}>"


def verify_written_tar(archive: pathlib.Path) -> pathlib.Path:
    """Full tzf walk of a just-written archive (same contract as backup_payload).

    Returns the absolute path on success. Exits non-zero if the archive cannot
    be listed — a pack that cannot be listed today must fail today.
    """
    r = subprocess.run(
        ["tar", "tzf", str(archive)], capture_output=True, text=True)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or f"rc {r.returncode}").strip()
        sys.exit(f"data tar verify failed: {archive} — {detail}")
    return archive.resolve()


def export_stream(name, stream_type, start_us, end_us, out_path):
    """Page SELECT * into JSONL. Returns row count; loud on the page cap."""
    rows = 0
    with open(out_path, "w") as fh:
        for page in range(MAX_PAGES):
            body = {"query": {"sql": f'SELECT * FROM "{name}"',
                              "start_time": start_us, "end_time": end_us,
                              "from": page * PAGE, "size": PAGE}}
            hits = req("POST", f"/_search?type={stream_type}", body).get("hits", [])
            for h in hits:
                fh.write(json.dumps(h, separators=(",", ":")) + "\n")
            rows += len(hits)
            if len(hits) < PAGE:
                return rows, False
    print(f"  ⚠ {name}: page cap hit at {rows} rows — window has MORE data; "
          f"re-run with a shorter --days per slice", file=sys.stderr)
    return rows, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default=None)
    ap.add_argument("--include-logs", action="store_true")
    args = ap.parse_args()

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M")
    out = pathlib.Path(args.out or os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
        "devcake", "receipts", f"receipts-{stamp}"))
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)

    end_us = int(time.time() * 1_000_000)
    start_us = end_us - args.days * 86_400 * 1_000_000

    # 1 — deploy identity
    manifest = {
        "exported_at_utc": stamp,
        "window_days": args.days,
        "git_describe": sh(["git", "describe", "--tags", "--always"]),
        "git_head": sh(["git", "rev-parse", "HEAD"]),
        "devcake_tag": env("DEVCAKE_TAG", "latest"),
        "images": sh(["docker", "images", "--digests",
                      "--format", "{{.Repository}}:{{.Tag}} {{.Digest}}"]),
        "compose_ps": sh(["docker", "compose", "ps"]),
        "streams": {},
        "skipped_log_streams": [],
    }

    # 2 — OO streams
    listing = req("GET", "/streams")
    streams = listing.get("list") or listing.get("data") or []
    for s in streams:
        name, stype = s.get("name"), s.get("stream_type", "traces")
        if stype != "traces" and not args.include_logs:
            manifest["skipped_log_streams"].append(f"{name} ({stype})")
            continue
        path = out / f"oo-{stype}-{name}.jsonl"
        print(f"exporting {stype}/{name} …")
        rows, capped = export_stream(name, stype, start_us, end_us, path)
        manifest["streams"][f"{stype}/{name}"] = {"rows": rows, "capped": capped}
        print(f"  {rows} rows → {path.name}")

    # 3 — sanitized /data (secrets EXCLUDED; volume-tar per backup_data.sh,
    # incl. its audit fixes: bind the DIRECTORY, basename via env, chown)
    volume = os.environ.get("DEVCAKE_DATA_VOLUME", "devcake_devcake_data")
    tar_base = "data-sans-secrets.tar.gz"
    rc = subprocess.run(
        ["docker", "run", "--rm",
         "-e", f"OUT_BASE={tar_base}",
         "-e", f"OWNER={os.getuid()}:{os.getgid()}",
         "-v", f"{volume}:/src:ro", "-v", f"{out}:/out", ALPINE_IMAGE,
         "sh", "-c",
         'umask 077 && tar czf "/out/$OUT_BASE" -C /src'
         ' --exclude ./secrets . && chown "$OWNER" "/out/$OUT_BASE"'],
    ).returncode
    if rc != 0:
        sys.exit(f"/data export failed (volume {volume}) — rc {rc}")

    verified = verify_written_tar(out / tar_base)

    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nreceipts pack: {out}")
    print(f"  verified data tar: {verified}")
    if manifest["skipped_log_streams"]:
        print(f"  log streams NOT exported (named in MANIFEST): "
              f"{len(manifest['skipped_log_streams'])} — use --include-logs")
    print("  private material (mission content) — store accordingly.")
    print("  this is NOT a backup: run backup_data.sh + backup_gitea.sh for that.")


if __name__ == "__main__":
    os.chdir(pathlib.Path(__file__).resolve().parent.parent)
    OO = env("OO_URL", "http://localhost:5080")
    ORG = env("OO_ORG", "default")
    AUTH = base64.b64encode(
        f"{env('OO_ROOT_EMAIL')}:{env('OO_ROOT_PASSWORD')}".encode()).decode()
    main()
