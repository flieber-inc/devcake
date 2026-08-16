# DevCake image matrix — single source of truth for ALL image builds.
# Compose only *runs* these tags; it does not build them.
#
#   docker buildx bake              # control plane: app + admin
#   docker buildx bake images       # Dev harnesses + hello stub
#   docker buildx bake all          # everything (first install / upgrade)
#   docker buildx bake app-test     # prod app + pytest (CI)
#
# Local BuildKit cache is OPT-IN: the default `docker` driver cannot export
# cache ("Cache export is not supported for the docker driver"), so a stock
# Docker Engine must be able to run the commands above as-is. To enable it,
# use a docker-container builder (docker buildx create --use) or the
# containerd image store, then:
#   BAKE_LOCAL_CACHE=1 docker buildx bake all     # exports to .buildx-cache/
#
# GitHub Actions — add the CI overlay so cache hits GHA:
#   docker buildx bake -f docker-bake.hcl -f docker-bake.ci.hcl all

variable "TAG" {
  default = "latest"
}

variable "DEVCAKE_TAG" {
  default = ""
}

variable "DEVCAKE_APP_DIGEST" {
  default = "DEVCAKE_APP_DIGEST_UNSET"
}

# Local export/import path for BuildKit cache (override if needed).
variable "BAKE_CACHE_DIR" {
  default = ".buildx-cache"
}

# Set to any nonempty value to enable the local cache export/import
# (requires a docker-container builder or the containerd image store).
variable "BAKE_LOCAL_CACHE" {
  default = ""
}

function "image_tag" {
  params = []
  result = DEVCAKE_TAG != "" ? DEVCAKE_TAG : TAG
}

group "default" {
  targets = ["app", "admin"]
}

group "images" {
  targets = ["hello", "claude-code", "codex", "grok-build", "pi", "opencode", "qwen-code"]
}

group "all" {
  targets = ["app", "admin", "hello", "claude-code", "codex", "grok-build", "pi", "opencode", "qwen-code"]
}

# Control plane + CI test image (no harnesses — faster PR loops).
group "ci" {
  targets = ["app", "app-test", "admin", "hello"]
}

target "_common" {
  args = {
    BUILDKIT_INLINE_CACHE = "1"
  }
  # Local persistent cache — survives daemon restarts better than memory alone.
  # Parallel targets share this dir; mode=max retains intermediate stages.
  # Opt-in (BAKE_LOCAL_CACHE=1): the default docker driver can't export cache.
  cache-from = BAKE_LOCAL_CACHE != "" ? ["type=local,src=${BAKE_CACHE_DIR}"] : []
  cache-to   = BAKE_LOCAL_CACHE != "" ? ["type=local,dest=${BAKE_CACHE_DIR},mode=max"] : []
}

target "app" {
  inherits   = ["_common"]
  context    = "./app"
  dockerfile = "Dockerfile"
  target     = "runtime"
  tags       = ["devcake/app:${image_tag()}"]
  args = {
    DEVCAKE_APP_DIGEST = DEVCAKE_APP_DIGEST
  }
}

target "app-test" {
  inherits   = ["_common"]
  context    = "./app"
  dockerfile = "Dockerfile"
  target     = "test"
  tags       = ["devcake/app-test:${image_tag()}"]
  args = {
    DEVCAKE_APP_DIGEST = DEVCAKE_APP_DIGEST
  }
}

target "admin" {
  inherits   = ["_common"]
  context    = "./admin"
  dockerfile = "Dockerfile"
  tags       = ["devcake/admin:${image_tag()}"]
}

target "dev-common" {
  inherits   = ["_common"]
  context    = "./images"
  dockerfile = "Dockerfile"
}

target "hello" {
  inherits = ["dev-common"]
  target   = "hello"
  tags     = ["devcake/dev-hello:${image_tag()}"]
}

target "claude-code" {
  inherits = ["dev-common"]
  target   = "claude-code"
  tags     = ["devcake/dev-claude-code:${image_tag()}"]
}

target "codex" {
  inherits = ["dev-common"]
  target   = "codex"
  tags     = ["devcake/dev-codex:${image_tag()}"]
}

target "grok-build" {
  inherits = ["dev-common"]
  target   = "grok-build"
  tags     = ["devcake/dev-grok-build:${image_tag()}"]
}

target "pi" {
  inherits = ["dev-common"]
  target   = "pi"
  tags     = ["devcake/dev-pi:${image_tag()}"]
}

target "opencode" {
  inherits = ["dev-common"]
  target   = "opencode"
  tags     = ["devcake/dev-opencode:${image_tag()}"]
}

target "qwen-code" {
  inherits = ["dev-common"]
  target   = "qwen-code"
  tags     = ["devcake/dev-qwen-code:${image_tag()}"]
}
