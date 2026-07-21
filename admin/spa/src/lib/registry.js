// Adapter registry (GET /api/v1/connections/registry): which PMO systems and
// forges this DevCake build knows, plus their secret shapes. Fetched once;
// the static fallback mirrors the v0 registry so the UI renders before (or
// without) the response.
import { get } from "../api.js";

const FALLBACK = {
  pmo_systems: [
    {
      id: "linear",
      display_name: "Linear",
      needs_api_base: false,
      team_key_label: "Team key",
      team_key_help:
        "The team's short key — the prefix of its issue IDs (PRJ for PRJ-123). This instance watches only this team. Empty = instance stays idle.",
      api_base_help: "",
    },
    {
      id: "gitea_issues",
      display_name: "Gitea Issues",
      needs_api_base: true,
      team_key_label: "Issues repo",
      team_key_help:
        "owner/repo of the dedicated issues board (e.g. devcake-pmo/missions). Empty = idle.",
      api_base_help:
        "Gitea origin from the app container. Bundled: http://gitea:3000. External: https://gitea.example.com",
    },
  ],
  forges: [
    { id: "github", display_name: "GitHub" },
    { id: "gitlab", display_name: "GitLab" },
    { id: "gitea", display_name: "Gitea" },
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
