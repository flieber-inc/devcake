// Per-PMO playbook override presentation helpers (CAKE-160).
// Draft contract is unchanged: presence of a non-empty Mission-Type key =
// override; empty / deleted key = inherit global. These helpers only drive
// collapse/expand chrome on the Prompts page.
import { MISSION_TYPES } from "./missionStages.js";

/** True when any Mission-Type key has a non-empty override value. */
export function pmoHasPromptOverride(pmo) {
  const overrides = pmo?.active_prompt_templates || {};
  return Object.keys(overrides).some((mt) => !!overrides[mt]);
}

/** Indexes of PMO boards that should auto-expand because they override. */
export function pmoOverrideExpandIndexes(pmos) {
  return (pmos || [])
    .map((p, i) => (pmoHasPromptOverride(p) ? i : -1))
    .filter((i) => i >= 0);
}

/**
 * One-line summary for a collapsed override row.
 * Fully inheriting → "all four inherit global — override…"
 * Partial → "overrides PLAN, REVIEW — edit…"
 */
export function pmoOverrideSummaryText(overrides) {
  const map = overrides || {};
  const set = MISSION_TYPES.filter((mt) => !!map[mt]);
  if (set.length === 0) return "all four inherit global — override…";
  return `overrides ${set.join(", ")} — edit…`;
}
