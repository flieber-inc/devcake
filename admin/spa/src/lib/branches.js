// Default-branch discovery on the Repositories page (ADR-0024 addendum).
// Pure helpers over the discover endpoints' results — no React, no DOM —
// covered by tests/discover_branch_helpers.mjs.

export const DISCOVER_ALL_BRANCHES_DESC =
  "Asks every saved repository which branch its HEAD names (one read-only git query per card). " +
  "Fills empty Branch fields and replaces pins the repository does not have; a pin the repository " +
  "has is kept. Changes land in the draft — review them on Save.";

// The draft edit the bulk discover makes.
export function applyDiscoveredBranches(repos, results) {
  const filled = [];
  const kept = [];
  const failed = [];
  const next = repos.map((r) => {
    const res = results[r.name];
    if (!res) return r;
    if (!res.ok) {
      failed.push({ name: r.name, error: res.error || "discovery failed" });
      return r;
    }
    const pin = (r.default_branch || "").trim();
    if (pin && res.pin_exists !== false) {
      kept.push({ name: r.name, pin, branch: res.branch });
      return r;
    }
    if (pin === res.branch) return r;
    filled.push({ name: r.name, from: pin, to: res.branch });
    return { ...r, default_branch: res.branch };
  });
  return { repos: next, changed: filled.length > 0,
    summary: { filled, kept, failed } };
}

