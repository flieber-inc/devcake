// CAKE-171: single phrasebook for absent token/cost figures on Runs.
// Callers choose this path when a scalar is null; measured values (incl. 0)
// still go through tokens()/usd(). Pure — no React.

const WAITING = new Set(["dispatched", "running", "finalizing"]);
const FAILED = new Set(["failed", "orphaned", "timed_out"]);

/** Short reasons safe to parenthesize after "not extracted". */
const ABSENCE_REASONS = new Set(["unavailable"]);

/**
 * @param {{ state?: string | null, source?: string | null }} opts
 * @returns {string} phrasebook copy for a null token/cost cell
 */
export function absenceCopy({ state, source } = {}) {
  if (WAITING.has(state)) return "available after the run ends";
  if (FAILED.has(state)) return "not extracted (run failed)";
  // finished, aggregates (pass state: "finished"), and any other terminal
  // success / unknown state: not extracted (+ short reason when known)
  if (source && ABSENCE_REASONS.has(source)) {
    return `not extracted (${source})`;
  }
  return "not extracted";
}
