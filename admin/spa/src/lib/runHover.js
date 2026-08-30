// CAKE-167: compact hover/detail copy for synthetic Runs-tab mission keys.
// Rows stay one line; extra identifiers live in native title= popups (and the
// RunTerminal header). Ordinary mission keys return "" — key + PMO link only.

const SYNTHETIC = {
  TEAM: "steward",
  HELLO: "hello smoke",
  OAUTH: "OAuth probe",
};

/** @param {{ mission_key?: string, pmo_ref?: string, steward_duty?: string, outcome_summary?: string }} run */
export function runHoverDetail(run) {
  if (!run || !run.mission_key) return "";
  const key = run.mission_key;
  const kind = SYNTHETIC[key];
  if (!kind) return "";

  const parts = [];
  if (key === "TEAM") {
    if (run.pmo_ref) parts.push(run.pmo_ref);
    parts.push(run.steward_duty === "discovery" ? "discovery" : "relations");
    if (run.outcome_summary) parts.push(run.outcome_summary);
  } else {
    parts.push(kind);
    if (run.pmo_ref) parts.push(run.pmo_ref);
  }
  return parts.join(" · ");
}
