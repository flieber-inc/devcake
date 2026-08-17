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

# No mandatory `-f docker-bake.hcl`: the default file in the repo root is that
# HCL, and buildah's buildx rejects `-f` even when bake itself is absent.
if docker buildx bake app-test; then
  echo "── app-test via: docker buildx bake app-test"
  exit 0
fi

echo "── docker buildx bake unavailable or failed; fallback: docker build --target test"
# Match docker-bake.hcl target "app-test" (context ./app, Dockerfile target test).
if docker build -f app/Dockerfile --target test \
  -t "devcake/app-test:${TAG}" \
  ./app; then
  echo "── app-test via: docker build -f app/Dockerfile --target test"
  exit 0
fi

echo "ERROR: could not build devcake/app-test:${TAG} (bake and Dockerfile fallback both failed)" >&2
exit 1
