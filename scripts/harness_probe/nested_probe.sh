#!/usr/bin/env bash
# Nested-engine rig receipt: replay the dev-run DAG's exact runtime contract
# for rootless podman inside a Dev container — the inline seccomp profile
# (extracted live from dagu/dags/dev-run.yaml, never a copy), the AppArmor
# profile name the stack runs under (DEVCAKE_APPARMOR_PROFILE from .env, as
# devcake up wrote it), Docker's masked/read-only system paths removed
# (systempaths=unconfined, the DAG's empty MaskedPaths/ReadonlyPaths), the
# run network, /dev/fuse + /dev/net/tun, the image's own dev user, the
# per-run /workspace bind, and the B1 reclaim chown — against a locally
# baked harness image. The host baker runs it after every harness bake and
# publishes the newest receipt as `nested` in the bake status (docs/11).
#
# One green run turns "matrix measured 2026-08-13, WSL2 kernel 6.6" (the one
# rig in images/Dockerfile) into a receipt for THIS rig; a red run names the
# first failing step instead of a shrug:
#   uid_map red   → the file-caps newuidmap fix does not transfer here
#   graph red     → storage driver setup (overlay-on-overlay / fuse fallback)
#   nested red    → full path (seccomp, the AppArmor profile, network)
#   ws uids       → bind ownership semantics (VirtioFS on macOS) + reclaim
#
# Usage: nested_probe.sh [image]   (default devcake/dev-claude-code:$DEVCAKE_TAG)
#   NESTED_TEST_IMAGE overrides the inner image (default docker.io/library/
#   alpine — the nested pull needs egress from inside the Dev container).
#   NESTED_PROBE_TIMEOUT caps the outer run in seconds (default 300).
#   DEVCAKE_APPARMOR_PROFILE overrides the .env value (default docker-default).
# Receipt: .factory/nested_probe/receipt-<utc>.json (log + sent profile beside
# it); the newest five receipts are kept.
set -euo pipefail
cd "$(dirname "$0")/../.."

IMAGE="${1:-devcake/dev-claude-code:${DEVCAKE_TAG:-latest}}"
NESTED_TEST_IMAGE="${NESTED_TEST_IMAGE:-docker.io/library/alpine}"

# Same namespace fence as host_probe.sh — this replays the bake contract,
# not arbitrary images.
if [[ ! "$IMAGE" =~ ^devcake/dev-[a-z0-9-]+:[A-Za-z0-9._-]+$ ]]; then
  echo "nested_probe: refusing image ${IMAGE@Q}" >&2
  exit 2
fi
if [[ "$IMAGE" == devcake/dev-hello:* ]]; then
  echo "nested_probe: hello does not carry the nested engine — pick a harness image" >&2
  exit 2
fi
# Local bake only — never Hub (audit A7).
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "nested_probe: ${IMAGE} is not local — bake it first (devcake images are never pulled)" >&2
  exit 2
fi

# The profile name the real runs use: devcake up derives it into .env and
# compose hands it to the DAG — the receipt must replay THAT value, so a
# process-env override is honored only when set on purpose.
# .env first (the value compose handed the DAG), then the process env,
# then Docker's default — the same order the baker resolves it in.
ENV_PROFILE=""
if [[ -f .env ]]; then
  ENV_PROFILE="$(python3 - <<'PYENV'
import re, sys
val = ""
for raw in open(".env", encoding="utf-8", errors="replace"):
    line = raw.strip().rstrip("\r")
    if not line or line.startswith("#"):
        continue
    if line.startswith("export "):
        line = line[7:].lstrip()
    m = re.match(r"DEVCAKE_APPARMOR_PROFILE\s*=\s*(.*)$", line)
    if m:
        v = m.group(1).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        val = v
sys.stdout.write(val)
PYENV
)"
fi
APPARMOR_PROFILE="${ENV_PROFILE:-${DEVCAKE_APPARMOR_PROFILE:-docker-default}}"
PROBE_TIMEOUT="${NESTED_PROBE_TIMEOUT:-300}"
# The DAG launches Dev containers on the run network; when the stack is
# up the receipt replays that too, otherwise Docker's default bridge.
RUN_NETWORK=""
if docker network inspect devcake_runtime >/dev/null 2>&1; then
  RUN_NETWORK="devcake_runtime"
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTDIR=".factory/nested_probe"
mkdir -p "$OUTDIR"
# Repo-local scratch, not mktemp: the repo sits inside every Docker Desktop
# file share, /var/folders may not.
WS="$OUTDIR/ws-$STAMP"
mkdir -p "$WS"
chmod 777 "$WS"     # the image's uid-1000 dev user must write; reclaimed below
WSABS="$(pwd)/$WS"
LOG="$OUTDIR/container-$STAMP.log"
RECEIPT="$OUTDIR/receipt-$STAMP.json"
SECCOMP="$OUTDIR/seccomp-$STAMP.json"

python3 scripts/harness_probe/nested_seccomp.py dagu/dags/dev-run.yaml > "$SECCOMP"
SECCOMP_SHA="$(python3 -c 'import hashlib,sys
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$SECCOMP")"

# Engine facts — on Docker Desktop these describe the LinuxKit VM, exactly
# the half no in-tree matrix row has ever covered.
ENGINE="$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)"
KERNEL="$(docker info --format '{{.KernelVersion}}' 2>/dev/null || echo unknown)"
HOST_OS="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || echo unknown)"
ARCH="$(docker info --format '{{.Architecture}}' 2>/dev/null || echo unknown)"
SECOPTS="$(docker info --format '{{join .SecurityOptions ","}}' 2>/dev/null || echo unknown)"

# The inner probe rides the workspace bind — no argv quoting to go wrong,
# and it exercises the same mount the real run uses. Never `set -e` inside:
# a red step must still name itself and let the later steps report.
cat > "$WS/np_inner.sh" <<'INNER'
echo "NP_UID=$(id -u)"
echo "NP_UNAME=$(uname -r)"
out="$(podman unshare cat /proc/self/uid_map 2>&1)"; rc=$?
echo "NP_UIDMAP_RC=$rc"
# first actual map row (podman may prepend warnings, e.g. shared-mount)
echo "NP_UIDMAP=$(printf '%s\n' "$out" \
  | grep -E -m1 '^[[:space:]]*[0-9]+[[:space:]]+[0-9]+[[:space:]]+[0-9]+' \
  | tr -s ' ')"
[ "$rc" -eq 0 ] || printf '%s\n' "$out" | sed 's/^/NP_UIDMAP_ERR: /'
out="$(podman info --format '{{.Store.GraphDriverName}}' 2>&1)"; rc=$?
echo "NP_GRAPH_RC=$rc"
echo "NP_GRAPH=$(printf '%s\n' "$out" | tail -1)"
out="$(podman run --rm -v /workspace:/w "$NP_TEST_IMAGE" sh -c 'id -u > /w/np_write && echo nested-ok' 2>&1)"; rc=$?
echo "NP_NESTED_RC=$rc"
printf '%s\n' "$out" | sed 's/^/NP_NESTED_OUT: /'
# Second write as a NON-root nested user: nested root maps back to the dev
# uid (1000 outside), so only this write exercises the subuid range — the
# foreign-uid files the B1 reclaim handler exists for, and the ownership
# semantics VirtioFS binds must get right on macOS.
out="$(podman run --rm --user 4321 -v /workspace:/w "$NP_TEST_IMAGE" sh -c 'echo x > /w/np_write_subuid' 2>&1)"; rc=$?
echo "NP_SUBUID_RC=$rc"
[ "$rc" -eq 0 ] || printf '%s\n' "$out" | sed 's/^/NP_SUBUID_ERR: /'
exit 0
INNER
chmod 644 "$WS/np_inner.sh"

# Mirror run_dev: image's own user + entrypoint overridden only so the full
# harness bootstrap (redis, runspec) is not required for an engine probe.
echo "── nested_probe: $IMAGE on ${HOST_OS} kernel ${KERNEL} (engine ${ENGINE}, ${ARCH}) apparmor=${APPARMOR_PROFILE} network=${RUN_NETWORK:-bridge}"
NET_ARGS=()
if [[ -n "$RUN_NETWORK" ]]; then NET_ARGS=(--network "$RUN_NETWORK"); fi
# Portable cap (no GNU timeout on stock macOS): a named container and a
# background watchdog that force-removes it — the daemon kills the whole
# process tree, so a wedged inner pull cannot outlive the probe.
NP_NAME="np-$STAMP"
( sleep "$PROBE_TIMEOUT"; docker rm -f "$NP_NAME" >/dev/null 2>&1 ) &
WATCHDOG=$!
set +e
docker run --rm --name "$NP_NAME" \
  --security-opt seccomp="$(pwd)/$SECCOMP" \
  --security-opt apparmor="$APPARMOR_PROFILE" \
  --security-opt systempaths=unconfined \
  ${NET_ARGS[@]+"${NET_ARGS[@]}"} \
  --device /dev/fuse --device /dev/net/tun \
  -e NP_TEST_IMAGE="$NESTED_TEST_IMAGE" \
  -v "$WSABS:/workspace" \
  --entrypoint /bin/sh \
  "$IMAGE" /workspace/np_inner.sh > "$LOG" 2>&1
DOCKER_RC=$?
set -e
if kill -0 "$WATCHDOG" 2>/dev/null; then
  kill "$WATCHDOG" 2>/dev/null; wait "$WATCHDOG" 2>/dev/null || true
else
  echo "NP_TIMEOUT=1 (outer run exceeded ${PROBE_TIMEOUT}s)" >> "$LOG"
fi
docker rm -f "$NP_NAME" >/dev/null 2>&1 || true
sed 's/^/   /' "$LOG"

np_uid() {
  python3 -c 'import os, sys; print(os.stat(sys.argv[1]).st_uid)' "$1" 2>/dev/null \
    || echo absent
}
WRITE_UID="$(np_uid "$WS/np_write")"
SUBUID_WRITE_UID="$(np_uid "$WS/np_write_subuid")"

# Mirror the DAG's exit handler verbatim: root, no network, -h chown.
set +e
docker run --rm --user 0 --network none \
  -v "$WSABS:/workspace" \
  --entrypoint /bin/sh \
  "$IMAGE" -c 'chown -R -h 1000:1000 /workspace || true' >> "$LOG" 2>&1
RECLAIM_RC=$?
set -e
RECLAIM_UID="$(np_uid "$WS/np_write")"
RECLAIM_SUBUID_UID="$(np_uid "$WS/np_write_subuid")"

val() { { grep -m1 "^$1=" "$LOG" || true; } | cut -d= -f2-; }
UIDMAP_RC="$(val NP_UIDMAP_RC)"
UIDMAP="$(val NP_UIDMAP)"
GRAPH_RC="$(val NP_GRAPH_RC)"
GRAPH="$(val NP_GRAPH)"
NESTED_RC="$(val NP_NESTED_RC)"
SUBUID_RC="$(val NP_SUBUID_RC)"
INNER_KERNEL="$(val NP_UNAME)"
INNER_UID="$(val NP_UID)"

NESTED_OK=false
if [[ "$DOCKER_RC" -eq 0 && "${NESTED_RC:-1}" = "0" ]] \
   && grep -q "^NP_NESTED_OUT: nested-ok$" "$LOG"; then
  NESTED_OK=true
fi
# The rig verdict also demands the B1 story: a non-root nested user could
# write the workspace, and the reclaim brought that file back to uid 1000 —
# anything else means WorkspaceStore leaks trees on this rig.
RIG_OK=false
if [[ "$NESTED_OK" = true && "${SUBUID_RC:-1}" = "0" \
      && "$RECLAIM_SUBUID_UID" = "1000" ]]; then
  RIG_OK=true
fi

# The first red step, named for the receipt (and for the surfaces that
# read it — the panel, `devcake status`, the Dev's prompt line).
FIRST_RED=""
if [[ "$RIG_OK" != true ]]; then
  if grep -q "^NP_TIMEOUT=1" "$LOG"; then
    FIRST_RED="the probe container did not finish within ${PROBE_TIMEOUT}s"
  elif [[ "$DOCKER_RC" -ne 0 && -z "${INNER_UID:-}" ]]; then
    FIRST_RED="the Dev container could not start ($(grep -m1 -i 'error' "$LOG" | cut -c1-200 || true))"
  elif [[ "${UIDMAP_RC:-1}" != "0" ]]; then
    FIRST_RED="the engine cannot create a user namespace (uid_map: $(grep -m1 '^NP_UIDMAP_ERR: ' "$LOG" | cut -d' ' -f2- | cut -c1-200 || true))"
  elif [[ "${GRAPH_RC:-1}" != "0" ]]; then
    FIRST_RED="nested storage setup failed ($(val NP_GRAPH | cut -c1-200))"
  elif [[ "${NESTED_RC:-1}" != "0" ]]; then
    FIRST_RED="a nested container run failed ($(grep -m1 '^NP_NESTED_OUT: ' "$LOG" | cut -d' ' -f2- | cut -c1-200 || true))"
  elif [[ "${SUBUID_RC:-1}" != "0" ]]; then
    FIRST_RED="a nested non-root user could not write the workspace bind"
  elif [[ "$RECLAIM_SUBUID_UID" != "1000" ]]; then
    FIRST_RED="the workspace reclaim left a nested write at uid ${RECLAIM_SUBUID_UID}"
  else
    FIRST_RED="see the probe log"
  fi
fi

NP_RECEIPT="$RECEIPT" NP_IMAGE="$IMAGE" NP_TEST_IMG="$NESTED_TEST_IMAGE" \
NP_STAMP="$STAMP" NP_ENGINE="$ENGINE" NP_KERNEL="$KERNEL" NP_HOST_OS="$HOST_OS" \
NP_ARCH="$ARCH" NP_SECOPTS="$SECOPTS" NP_SECCOMP_SHA="$SECCOMP_SHA" \
NP_APPARMOR="$APPARMOR_PROFILE" NP_NETWORK="${RUN_NETWORK:-bridge}" \
NP_FIRST_RED="$FIRST_RED" NP_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null || echo unknown)" \
NP_DOCKER_RC="$DOCKER_RC" NP_INNER_UID="${INNER_UID:-}" \
NP_INNER_KERNEL="${INNER_KERNEL:-}" NP_UIDMAP_RC="${UIDMAP_RC:-}" \
NP_UIDMAP="${UIDMAP:-}" NP_GRAPH_RC="${GRAPH_RC:-}" NP_GRAPH="${GRAPH:-}" \
NP_NESTED_RC="${NESTED_RC:-}" NP_NESTED_OK="$NESTED_OK" \
NP_SUBUID_RC="${SUBUID_RC:-}" NP_SUBUID_WRITE_UID="$SUBUID_WRITE_UID" \
NP_RECLAIM_SUBUID_UID="$RECLAIM_SUBUID_UID" NP_RIG_OK="$RIG_OK" \
NP_WRITE_UID="$WRITE_UID" NP_RECLAIM_UID="$RECLAIM_UID" \
NP_RECLAIM_RC="$RECLAIM_RC" \
python3 - <<'PY'
import json
import os

e = os.environ
receipt = {
    "measured_at": e["NP_STAMP"],
    "image": e["NP_IMAGE"],
    "nested_test_image": e["NP_TEST_IMG"],
    "host": {"engine": e["NP_ENGINE"], "kernel": e["NP_KERNEL"],
             "os": e["NP_HOST_OS"], "arch": e["NP_ARCH"],
             "security_options": e["NP_SECOPTS"]},
    "seccomp_sha256": e["NP_SECCOMP_SHA"],
    "apparmor_profile": e["NP_APPARMOR"],
    "network": e["NP_NETWORK"],
    "image_id": e["NP_IMAGE_ID"],
    "first_red": e["NP_FIRST_RED"],
    "container": {"uid": e["NP_INNER_UID"], "kernel": e["NP_INNER_KERNEL"]},
    "docker_run_rc": int(e["NP_DOCKER_RC"]),
    "uid_map": {"rc": e["NP_UIDMAP_RC"], "first_row": e["NP_UIDMAP"]},
    "graph_driver": {"rc": e["NP_GRAPH_RC"], "name": e["NP_GRAPH"]},
    "nested_run": {"rc": e["NP_NESTED_RC"], "ok": e["NP_NESTED_OK"] == "true"},
    "workspace_bind": {
        # nested ROOT maps back to the dev uid; the subuid row is the
        # foreign-uid case the B1 reclaim handler exists for
        "uid_after_nested_root_write": e["NP_WRITE_UID"],
        "subuid_write_rc": e["NP_SUBUID_RC"],
        "uid_after_nested_subuid_write": e["NP_SUBUID_WRITE_UID"],
        "uid_after_reclaim_root_file": e["NP_RECLAIM_UID"],
        "uid_after_reclaim_subuid_file": e["NP_RECLAIM_SUBUID_UID"],
        "reclaim_rc": int(e["NP_RECLAIM_RC"]),
    },
    "rig_ok": e["NP_RIG_OK"] == "true",
}
with open(e["NP_RECEIPT"], "w", encoding="utf-8") as f:
    json.dump(receipt, f, indent=2, sort_keys=True)
    f.write("\n")
PY

echo
if [[ "$RIG_OK" = true ]]; then
  echo "── nested_probe: PASS — receipt: $RECEIPT"
else
  echo "── nested_probe: FAIL — first red NP_ step above; log: $LOG" >&2
fi
echo "matrix row: $STAMP | ${HOST_OS} kernel=${KERNEL} engine=${ENGINE} ${ARCH} apparmor=${APPARMOR_PROFILE} | graph=${GRAPH:-?} | uid_map_rc=${UIDMAP_RC:-?} nested_ok=${NESTED_OK} | ws uid root ${WRITE_UID}→${RECLAIM_UID} subuid ${SUBUID_WRITE_UID}→${RECLAIM_SUBUID_UID}"
rm -rf "$WS" 2>/dev/null \
  || echo "nested_probe: scratch left behind (foreign uids — reclaim red?): $WS" >&2
# Keep the newest five receipts (with their logs and sent profiles); the
# baker runs this after every bake, so the directory would otherwise grow
# forever.
ls -1t "$OUTDIR"/receipt-*.json 2>/dev/null | tail -n +6 | while read -r old; do
  stamp="${old##*/receipt-}"; stamp="${stamp%.json}"
  rm -f "$old" "$OUTDIR/container-$stamp.log" "$OUTDIR/seccomp-$stamp.json"
done
[[ "$RIG_OK" = true ]]
