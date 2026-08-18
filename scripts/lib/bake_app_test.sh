#!/usr/bin/env bash
# Chokepoint: tree-fresh devcake/app-test:${DEVCAKE_TAG:-latest}.
#
# Prefer Docker Buildx bake (real Docker Engine + Buildx). When bake is missing
# or rejects the CLI (e.g. podman/buildah's buildx shim: no `bake` subcommand,
# and `-f` is an "unknown shorthand flag"), fall back to the same image the
# bake target builds:
#   docker build -f app/Dockerfile --target test -t devcake/app-test:$TAG ./app
#
# Logs which path ran so a green unit suite is never misread as "full bake
# matrix green." Does NOT build admin/hello/harnesses.
#
# Callers: scripts/pytest_app.sh, scripts/ci_suite.sh. GHA uses docker/bake-action
# (real Buildx) and does not need this helper.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

TAG="${DEVCAKE_TAG:-latest}"
export DEVCAKE_TAG="$TAG"

echo "── ensure app-test → devcake/app-test:${TAG}"

# `-f docker-bake.hcl` is mandatory: without it, bake's default-file lookup
# also merges docker-compose.yml, whose `:?` interpolations hard-fail on a
# fresh clone with no .env. (On podman, buildah's buildx shim fails with or
# without `-f`, so the fallback below still triggers there either way.)
if docker buildx bake -f docker-bake.hcl app-test; then
  echo "── app-test via: docker buildx bake -f docker-bake.hcl app-test"
  exit 0
fi

echo "── bake failed (CLI missing or bake error); fallback: docker build --target test"
# Match docker-bake.hcl target "app-test" (context ./app, Dockerfile target test).
if docker build -f app/Dockerfile --target test \
  -t "devcake/app-test:${TAG}" \
  ./app; then
  echo "── app-test via: docker build -f app/Dockerfile --target test"
  exit 0
fi

echo "ERROR: could not build devcake/app-test:${TAG} (bake and Dockerfile fallback both failed)" >&2
exit 1
