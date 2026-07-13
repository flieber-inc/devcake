// Adapter registry (GET /api/v1/connections/registry): which PMO systems and
// forges this DevCake build knows, plus their secret shapes. Fetched once;
// the static fallback mirrors the v0 registry so the UI renders before (or
// without) the response.
import { get } from "../api.js";

const FALLBACK = {
  pmo_systems: [
    { id: "linear", display_name: "Linear", api_key_env_default: "LINEAR_API_KEY" },
  ],
  forges: [
    { id: "github", display_name: "GitHub", token_env_default: "GITHUB_TOKEN" },
    { id: "gitlab", display_name: "GitLab", token_env_default: "GITLAB_TOKEN" },
  ],
  secret_shape_prefixes: ["ghp_", "github_pat_", "glpat-", "lin_api_", "lin_oauth_"],
  managed_labels_expected: 10,
};

let cached = null;
let inflight = null;

export function getRegistry() {
  return cached || FALLBACK;
}

export function loadRegistry() {
  if (cached) return Promise.resolve(cached);
  if (!inflight) {
    inflight = get("/connections/registry")
      .then((r) => (cached = r))
      .catch(() => (cached = FALLBACK));
  }
  return inflight;
}
