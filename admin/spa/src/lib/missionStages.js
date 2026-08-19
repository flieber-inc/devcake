// Pinned mirror of domain/model.MissionType (ADR-0034 / spa-contracts).
// Do not re-spell this array — contracts.mjs + gen_spa_contracts pin it.

export const MISSION_STAGES = ["ONBOARD", "PLAN", "EXECUTE", "REVIEW"];

/** Alias kept for call sites that historically used STAGES / MISSION_TYPES. */
export const STAGES = MISSION_STAGES;
export const MISSION_TYPES = MISSION_STAGES;
