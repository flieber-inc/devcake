// Draft-save validation, DEPENDENCY-FREE on purpose: the hermetic helper
// suite (tests/config_draft.mjs — CI runs it with NO node_modules) grades
// draftErrors directly, so nothing here may import react or any package.
// The instance-name constants live here for the same reason;
// instanceNames.js re-exports them for its react-side consumers.

// Client-side mirror of the server's instance-name rule (config.py
// _INSTANCE_NAME_RE): the draft validates names BEFORE Save, so a bad name
// surfaces inline instead of as a raw 422 from the config PUT.
export const INSTANCE_NAME_RE = /^[a-z][a-z0-9]{0,11}$/;
export const INSTANCE_NAME_RULE =
  "a lowercase letter, then lowercase letters/digits, 12 max (no spaces, no hyphens)";


export function draftErrors(draft) {
  const errs = {};
  if (!draft) return errs;
  const rm = draft.cfg.steward || {};
  if (rm.enabled && !rm.dev_type)
    errs["cfg.steward.enabled"] =
      "Relations Steward: periodic service is ON but no Dev Type is selected";
  for (const [mt, a] of Object.entries(draft.assignments || {})) {
    if (a?.dev_type && !draft.devTypes[a.dev_type])
      errs[`assignments.${mt}.dev_type`] =
        `${mt} is assigned to "${a.dev_type}", which no longer exists`;
  }
  for (const [name, dt] of Object.entries(draft.devTypes || {})) {
    if (!Number.isFinite(dt.max_concurrency) || dt.max_concurrency < 1)
      errs[`devTypes.${name}.max_concurrency`] =
        `${name}: max concurrency must be ≥ 1`;
    // shape + duplicates mirror the server validator so a bad name blocks
    // Save inline instead of surfacing as the config PUT's raw 422 (the
    // reserved-name blocklist stays server-side only — no JS copy to
    // drift)
    const seenVars = new Set();
    for (const v of dt.secret_env || []) {
      if (!/^[A-Z][A-Z0-9_]{0,63}$/.test(v))
        errs[`devTypes.${name}.secret_env`] =
          `${name}: secret env var "${v}" must be UPPER_SNAKE_CASE`;
      else if (seenVars.has(v))
        errs[`devTypes.${name}.secret_env`] =
          `${name}: duplicate secret env var "${v}"`;
      seenVars.add(v);
    }
  }
  // instance names are validated in the draft so a bad one blocks Save with
  // an inline message instead of surfacing as the config PUT's raw 422
  const checkNames = (rows, key, kind) => {
    const seen = new Set();
    (rows || []).forEach((r, i) => {
      if (!INSTANCE_NAME_RE.test(r.name || ""))
        errs[`cfg.${key}.${i}.name`] =
          `${kind} name ${r.name ? `"${r.name}"` : "(empty)"} is invalid — ${INSTANCE_NAME_RULE}`;
      else if (seen.has(r.name))
        errs[`cfg.${key}.${i}.name`] = `duplicate ${kind} name "${r.name}"`;
      seen.add(r.name);
    });
  };
  checkNames(draft.cfg.repos, "repos", "repository");
  checkNames(draft.cfg.pmos, "pmos", "PMO instance");
  // container limits (2026-08-13 audit): Number("") === 0 and 0 means
  // UNLIMITED — a cleared field must block Save inline, never silently
  // uncap. The section stores a cleared box as null; same
  // mirror-the-server-validator rationale as the checks above.
  const cl = draft.cfg.container_limits;
  for (const [key, label] of [["memory_mb", "Memory (MB)"],
                              ["cpus", "CPUs"], ["pids", "PIDs"]]) {
    const v = cl ? cl[key] : undefined;
    if (v === undefined) continue;       // server default fills the key
    if (v === null || !Number.isFinite(v) || v < 0)
      errs[`cfg.container_limits.${key}`] =
        `Dev containers: ${label} needs a number — type 0 for unlimited`;
  }
  // a PMO listing a repo name with no card is refused by the server —
  // surface it inline (removals normally cascade; this catches stragglers)
  const repoNames = new Set((draft.cfg.repos || []).map((r) => r.name));
  (draft.cfg.pmos || []).forEach((p, i) => {
    for (const [field, label] of
         [["repos", "work repo"], ["reference_repos", "reference repo"],
          ["memory_repos", "memory notebook"]]) {
      const missing = (p[field] || []).filter((n) => !repoNames.has(n));
      if (missing.length)
        errs[`cfg.pmos.${i}.${field}`] =
          `PMO "${p.name}" lists removed ${label}${missing.length > 1 ? "s" : ""} ` +
          `${missing.map((m) => `"${m}"`).join(", ")} — deselect there or re-add the repo`;
    }
    // ADR-0019 assignment overrides — mirror the global-map check above
    for (const [mt, a] of Object.entries(p.assignments || {})) {
      if (a?.dev_type && !draft.devTypes[a.dev_type])
        errs[`cfg.pmos.${i}.assignments.${mt}.dev_type`] =
          `PMO "${p.name}": ${mt} override is assigned to "${a.dev_type}", which no longer exists`;
    }
  });
  return errs;
}
