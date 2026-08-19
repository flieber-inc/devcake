// Pinned mirror of domain/run.py TERMINAL_STATES (ADR-0034 / spa-contracts).
// Do not re-spell these arrays — contracts.mjs + gen_spa_contracts pin them.

export const TERMINAL_STATES = ["finished", "failed", "timed_out", "orphaned"];

// UI "stopped for streaming": terminal ∪ finalizing (MissionDrawer Stop hide).
// Distinct from TERMINAL_STATES — finalizing is not a terminal RunState.
export const STOPPED_STATES = [...TERMINAL_STATES, "finalizing"];
