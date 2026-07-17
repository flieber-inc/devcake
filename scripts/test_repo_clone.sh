#!/usr/bin/env bash
# Smoke-test a forge HTTPS clone the same way the Dev entrypoint does for
# primary + reference (extra_repos) clones: GIT_ASKPASS + clone_user in URL +
# DEVCAKE_FORGE_TOKEN (never embedded as the password in a stored remote).
#
# Usage:
#   # Resolve a named RepoInstance from the running stack (stored secrets):
#   ./scripts/test_repo_clone.sh <repo-instance-name>
#
#   # Or pass credentials explicitly (no stack required):
#   ./scripts/test_repo_clone.sh --url https://gitlab.com/group/proj \
#       --token "$GLPAT" --forge gitlab
#
#   # Options:
#   ./scripts/test_repo_clone.sh docs --deep          # full clone (default: --depth 1)
#   ./scripts/test_repo_clone.sh docs --token-write    # force write token (default: RO preferred)
#
# Exit 0 = clone succeeded; non-zero = failure (stderr has git output).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

URL=""
TOKEN=""
FORGE=""
CLONE_USER=""
REPO_NAME=""
DEEP=0
TOKEN_PREF="ro"   # ro = token_ro || token (same as _extra_repos_for)
IMAGE="${CLONE_TEST_IMAGE:-devcake/dev-claude-code:${DEVCAKE_TAG:-latest}}"

usage() {
  cat <<'EOF'
Smoke-test a forge HTTPS clone (same path as Dev entrypoint / extra_repos).

  ./scripts/test_repo_clone.sh <repo-instance-name>
  ./scripts/test_repo_clone.sh --url URL --token TOKEN [--forge gitlab|github|gitea]

Options:
  --deep          full clone (default: --depth 1, like reference clones)
  --token-write   use write token instead of RO-preferred
  --image IMAGE   default: devcake/dev-claude-code:${DEVCAKE_TAG:-latest}

Env:
  CLONE_TEST_IMAGE    override harness image
  CLONE_TEST_NETWORK  docker network (e.g. devcake_runtime for internal Gitea)
  DEVCAKE_TAG         image tag pin
EOF
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage ;;
    --url) URL="${2:?}"; shift 2 ;;
    --token) TOKEN="${2:?}"; shift 2 ;;
    --forge) FORGE="${2:?}"; shift 2 ;;
    --clone-user) CLONE_USER="${2:?}"; shift 2 ;;
    --deep) DEEP=1; shift ;;
    --token-write) TOKEN_PREF="write"; shift ;;
    --image) IMAGE="${2:?}"; shift 2 ;;
    -*)
      echo "unknown option: $1" >&2
      usage
      ;;
    *)
      if [[ -n "$REPO_NAME" ]]; then
        echo "unexpected argument: $1" >&2
        usage
      fi
      REPO_NAME="$1"
      shift
      ;;
  esac
done

clone_user_for_forge() {
  case "$1" in
    github) echo x-access-token ;;
    gitlab) echo oauth2 ;;
    gitea)  echo devcake ;;
    *)      echo x-access-token ;;
  esac
}

# ── resolve from running app (named RepoInstance) ───────────────────────────
if [[ -n "$REPO_NAME" ]]; then
  if [[ -n "$URL" || -n "$TOKEN" ]]; then
    echo "pass either <repo-name> OR --url/--token, not both" >&2
    exit 2
  fi
  if ! docker compose exec -T app true 2>/dev/null; then
    echo "app container not reachable — start the stack (docker compose up -d) or use --url/--token" >&2
    exit 1
  fi
  RESOLVED="$(docker compose exec -T app python - "$REPO_NAME" "$TOKEN_PREF" <<'PY'
import json, sys
from devcake.config import load_config
from devcake.adapters.registry import forges

name, pref = sys.argv[1], sys.argv[2]
cfg = load_config()
repo = next((r for r in cfg.repos if r.name == name), None)
if repo is None:
    print(f"no repo instance named {name!r}", file=sys.stderr)
    print("configured:", ", ".join(r.name for r in cfg.repos) or "(none)", file=sys.stderr)
    sys.exit(1)
if not repo.configured:
    print(f"repo {name!r} has empty URL", file=sys.stderr)
    sys.exit(1)

ro, write = repo.token_ro, repo.token
if pref == "write":
    token, source = write, "token"
else:
    token, source = (ro or write), ("token_ro" if ro else "token")
if not token:
    print(f"repo {name!r}: no stored token (need token_ro and/or token on the Config card)",
          file=sys.stderr)
    sys.exit(1)

desc = forges().get(repo.forge)
clone_user = desc.clone_user if desc else "x-access-token"

refs, works = [], []
for p in cfg.pmos:
    if name in (p.reference_repos or []):
        refs.append(p.name)
    if name in (p.repos or []):
        works.append(p.name)

print(json.dumps({
    "name": name,
    "url": repo.url,
    "forge": repo.forge,
    "clone_user": clone_user,
    "token_source": source,
    "reference_only": repo.reference_only,
    "listed_as_reference_on": refs,
    "listed_as_work_on": works,
    "token": token,
}))
PY
)" || exit 1

  _j() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d$1)" <<<"$RESOLVED"; }
  URL="$(_j "['url']")"
  FORGE="$(_j "['forge']")"
  CLONE_USER="$(_j "['clone_user']")"
  TOKEN="$(_j "['token']")"
  TOKEN_SOURCE="$(_j "['token_source']")"
  REF_ONLY="$(_j "['reference_only']")"
  REF_ON="$(python3 -c 'import json,sys; print(",".join(json.load(sys.stdin)["listed_as_reference_on"]) or "-")' <<<"$RESOLVED")"
  WORK_ON="$(python3 -c 'import json,sys; print(",".join(json.load(sys.stdin)["listed_as_work_on"]) or "-")' <<<"$RESOLVED")"

  echo "── resolve  repo=${REPO_NAME}  forge=${FORGE}  reference_only=${REF_ONLY}"
  echo "   url=${URL}"
  echo "   token_source=${TOKEN_SOURCE}  (same rule as Dev extra_repos: RO preferred)"
  echo "   listed_as_reference_on=[${REF_ON}]  listed_as_work_on=[${WORK_ON}]"
  if [[ "$REF_ON" == "-" && "$WORK_ON" == "-" ]]; then
    echo "   ⚠ not attached to any PMO instance — entrypoint will never clone it as a reference" >&2
  fi
elif [[ -n "$URL" && -n "$TOKEN" ]]; then
  FORGE="${FORGE:-github}"
  CLONE_USER="${CLONE_USER:-$(clone_user_for_forge "$FORGE")}"
  TOKEN_SOURCE="cli"
  echo "── manual  forge=${FORGE}  clone_user=${CLONE_USER}"
  echo "   url=${URL}"
else
  echo "need <repo-instance-name> or --url + --token" >&2
  usage
fi

# ── clone inside a throwaway Dev image (git + egress, same as harness) ──────
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "image $IMAGE not found — bake a harness (docker buildx bake images) or set CLONE_TEST_IMAGE" >&2
  exit 1
fi

SLUG="$(python3 -c 'import sys; u=sys.argv[1].rstrip("/"); print(u.rsplit("/",1)[-1].removesuffix(".git"))' "$URL")"
if [[ "$DEEP" -eq 1 ]]; then
  DEPTH_LABEL="full"
else
  DEPTH_LABEL="shallow (--depth 1, like reference/extra_repos)"
fi

echo "── clone   image=${IMAGE}  mode=${DEPTH_LABEL}  target=/tmp/repo/${SLUG}"
echo "   (mirrors images/common/dev_entrypoint.py clone_extra_repos)"

NET_ARGS=()
if [[ -n "${CLONE_TEST_NETWORK:-}" ]]; then
  NET_ARGS=(--network "$CLONE_TEST_NETWORK")
fi

set +e
OUT="$(docker run --rm "${NET_ARGS[@]}" \
  -e DEVCAKE_FORGE_TOKEN="$TOKEN" \
  -e CLONE_URL="$URL" \
  -e CLONE_USER="$CLONE_USER" \
  -e CLONE_SLUG="$SLUG" \
  -e CLONE_DEEP="$DEEP" \
  --entrypoint bash "$IMAGE" -c '
set -euo pipefail
ASKPASS=/tmp/devcake-askpass.sh
printf "#!/bin/sh\necho \"\$DEVCAKE_FORGE_TOKEN\"\n" > "$ASKPASS"
chmod 700 "$ASKPASS"
export GIT_ASKPASS="$ASKPASS" GIT_TERMINAL_PROMPT=0
if [[ -n "${CLONE_USER:-}" ]]; then
  case "$CLONE_URL" in
    https://*) CLONE_URL="https://${CLONE_USER}@${CLONE_URL#https://}" ;;
    http://*)  CLONE_URL="http://${CLONE_USER}@${CLONE_URL#http://}" ;;
  esac
fi
mkdir -p /tmp/repo
DEPTH=()
[[ "${CLONE_DEEP:-0}" == "1" ]] || DEPTH=(--depth 1)
echo "+ git clone ${DEPTH[*]:-} <redacted-url> /tmp/repo/${CLONE_SLUG}"
git clone "${DEPTH[@]}" "$CLONE_URL" "/tmp/repo/${CLONE_SLUG}"
HEAD=$(git -C "/tmp/repo/${CLONE_SLUG}" rev-parse --short HEAD)
BRANCH=$(git -C "/tmp/repo/${CLONE_SLUG}" rev-parse --abbrev-ref HEAD)
COUNT=$(git -C "/tmp/repo/${CLONE_SLUG}" ls-files | wc -l | tr -d " ")
echo "OK head=${HEAD} branch=${BRANCH} tracked_files=${COUNT}"
' 2>&1)"
RC=$?
set -e

# Strip userinfo from any URL git printed (token never should appear; username can).
SAFE_OUT="$(printf '%s\n' "$OUT" | sed -E 's#(https?://)[^@/[:space:]]+@#\1<user>@#g')"
printf '%s\n' "$SAFE_OUT"

if [[ $RC -eq 0 ]]; then
  echo "── result  CLONE OK (token_source=${TOKEN_SOURCE:-cli})"
  echo "   If a Dev still cannot re-clone mid-run: ambient DEVCAKE_FORGE_TOKEN is the"
  echo "   *work* repo token only; reference tokens are one-shot at entrypoint."
  exit 0
fi

echo "── result  CLONE FAILED (exit ${RC}, token_source=${TOKEN_SOURCE:-cli})" >&2
echo "   Check: token scopes, project path, SSO, and that this is the RO PAT from the" >&2
echo "   reference repo card (not the work-repo write PAT)." >&2
if printf '%s\n' "$SAFE_OUT" | grep -qiE 'not allowed to download code|read_repository'; then
  echo >&2
  echo "   GitLab hint: admin Config 'Test' only GETs the project API (can_read)." >&2
  echo "   git clone needs the PAT scope read_repository (plus read_api is not enough)." >&2
  echo "   Create a new glpat with read_repository and re-paste token_ro on the card." >&2
fi
exit "$RC"
