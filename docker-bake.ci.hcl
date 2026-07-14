# CI overlay for docker-bake.hcl — GitHub Actions BuildKit cache (type=gha).
#
#   docker buildx bake -f docker-bake.hcl -f docker-bake.ci.hcl all
#   docker buildx bake -f docker-bake.hcl -f docker-bake.ci.hcl ci
#
# Requires actions/cache + docker/setup-buildx-action (or equivalent) so
# type=gha can read/write. Does not apply to local Desktop builds — omit this
# file outside CI.

target "_common" {
  cache-from = ["type=gha,scope=devcake"]
  cache-to   = ["type=gha,mode=max,scope=devcake"]
}
