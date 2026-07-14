# DevCake image matrix — single source of truth for ALL image builds.
# Compose only *runs* these tags; it does not build them.
#
#   docker buildx bake              # control plane: app + admin
#   docker buildx bake images       # Dev harnesses + hello stub
#   docker buildx bake all          # everything (first install / upgrade)
#   TAG=$(git rev-parse --short HEAD) docker buildx bake all
#
# Optional: TAG / DEVCAKE_TAG (same value) so bake tags match compose:
#   export DEVCAKE_TAG=latest   # default
#   docker buildx bake all && docker compose up -d

variable "TAG" {
  default = "latest"
}

# Allow DEVCAKE_TAG from .env / shell to drive the same pin as compose.
variable "DEVCAKE_TAG" {
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
  targets = ["hello", "claude-code", "codex", "grok-build"]
}

group "all" {
  targets = ["app", "admin", "hello", "claude-code", "codex", "grok-build"]
}

target "_common" {
  # Shared defaults for every DevCake-built image.
  args = {
    BUILDKIT_INLINE_CACHE = "1"
  }
}

target "app" {
  inherits   = ["_common"]
  context    = "./app"
  dockerfile = "Dockerfile"
  tags       = ["devcake/app:${image_tag()}"]
}

target "admin" {
  inherits   = ["_common"]
  context    = "./admin"
  dockerfile = "Dockerfile"
  tags       = ["devcake/admin:${image_tag()}"]
}

# Shared BuildKit stages live in images/Dockerfile (base, node-tools, …).
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
