// Pinned mirror of secrets.CONNECTION_FIELDS (ADR-0034 / spa-contracts).
// Field lists are sorted per scope to match the committed JSON. Do not
// re-spell — contracts.mjs + gen_spa_contracts pin them.

export const CONNECTION_FIELDS = {
  pmo: ["api_key"],
  repo: ["reviewer_token", "token", "token_ro"],
  skill: ["token", "token_ro"],
};

// Operator-facing labels over the same vocabulary — ONE spelling for every
// dialog that names a slot (clear-secrets, token copy, …).
export const CONNECTION_FIELD_LABELS = {
  api_key: "API key",
  token: "Access token",
  token_ro: "Read-only token",
  reviewer_token: "Reviewer token",
};

/** Build `scope:instance:field`. Throws on an unknown scope/field so a
 *  rename on the server cannot silently assemble a silently-dropped ref. */
export function connRef(scope, instance, field) {
  const fields = CONNECTION_FIELDS[scope];
  if (!fields || !fields.includes(field)) {
    throw new Error(`unknown connection field ${scope}:${field}`);
  }
  return `${scope}:${instance}:${field}`;
}
