// CAKE-171: single phrasebook for absent token/cost figures on Runs.
// Callers choose this path when a scalar is null; measured values (incl. 0)
// still go through tokens()/usd(). Pure — no React.
// CAKE-174: costAbsenceCopy composes empty-card taxonomy with the same
// waiting/failed sets — do not fork a second state table in RunsPage.

const WAITING = new Set(["dispatched", "running", "finalizing"]);
const FAILED = new Set(["failed", "orphaned", "timed_out"]);

/** Short reasons safe to parenthesize after "not extracted". */
const ABSENCE_REASONS = new Set(["unavailable"]);

const EMPTY_RATE_CARD_COPY = "no rate card — add rates under Cost inputs";

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

/**
 * Cost-cell absence when effective cost is null.
 * Waiting/failed keep CAKE-171 timing honesty (native harness cost may still
 * arrive with an empty operator card). Empty-card taxonomy applies only when
 * cost is expected to be missing for rate reasons (finished / other).
 *
 * @param {{ state?: string | null, source?: string | null, emptyRateCard?: boolean }} opts
 * @returns {string}
 */
export function costAbsenceCopy({ state, source, emptyRateCard } = {}) {
  if (WAITING.has(state) || FAILED.has(state)) {
    return absenceCopy({ state, source });
  }
  if (emptyRateCard) return EMPTY_RATE_CARD_COPY;
  return absenceCopy({ state, source });
}
