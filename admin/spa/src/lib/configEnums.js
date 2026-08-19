// Pinned mirrors of AppConfig continuation_policy / attempt_reset Literals
// (ADR-0034 / spa-contracts). Do not re-spell these arrays — contracts.mjs
// + gen_spa_contracts pin them.

export const CONTINUATION_POLICIES = [
  "auto", "fresh-only", "off", "resume-only",
];

export const ATTEMPT_RESET_POLICIES = [
  "any-comment", "label-ops", "unlimited",
];
