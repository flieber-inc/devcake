import { useMemo, useRef, useState } from "react";
import { diffLeaves, setIn, applyEditsOnto } from "./objectPath.js";
import { INSTANCE_NAME_RE, INSTANCE_NAME_RULE } from "./instanceNames.js";

// Paths that never dirty the draft: server-owned state that immediate
// actions (sidebar intake switch, alert dismissal, credential uploads)
// change underneath an open draft.
const IGNORED = [
  /^cfg\.intake_paused$/,
  /^cfg\.dismissed_alerts$/,
  // Cost Inputs modal on the Runs page writes {cost_inputs} instantly
  // (ADR-0021) — a stale open draft must never clobber it on Save
  /^cfg\.cost_inputs/,
  /^cfg\.schema_version$/,
  /^devTypes\.[^.]+\.secrets_present$/,
  /^devTypes\.[^.]+\.secret_env_present$/,
];
const ignored = (path) => IGNORED.some((re) => re.test(path));

function normalize(cfg, devTypesArr, assignments) {
  return {
    // seed the ADR-0019 override map on every PMO card (server and draft
    // snapshots alike): diffs stay per-row and removing the last override
    // round-trips to a clean draft, even against a backend predating the
    // field
    cfg: { ...cfg,
           pmos: (cfg.pmos || []).map((p) => ({ assignments: {}, ...p })) },
    devTypes: Object.fromEntries(devTypesArr.map((d) => [d.name, d])),
    assignments,
  };
}

// Unified draft over {cfg, devTypes, assignments}. Nothing persists until the
// page-level Save; immediate actions refetch and `rebase` re-applies the
// user's edited leaves onto the fresh snapshot (edits win; edits under a
// deleted container are dropped).
export default function useConfigDraft() {
  const [server, setServer] = useState(null);
  const [draft, setDraft] = useState(null);
  const [order, setOrder] = useState([]); // dev type render order (server order)
  const serverRef = useRef(null);
  const draftRef = useRef(null);
  serverRef.current = server;
  draftRef.current = draft;

  const load = (cfg, devTypesArr, assignments) => {
    const snap = normalize(cfg, devTypesArr, assignments);
    setServer(snap);
    setDraft(snap);
    setOrder(devTypesArr.map((d) => d.name));
  };

  const rebase = (cfg, devTypesArr, assignments) => {
    const fresh = normalize(cfg, devTypesArr, assignments);
    const prevServer = serverRef.current;
    const prevDraft = draftRef.current;
    let next = fresh;
    if (prevServer && prevDraft) {
      const edits = diffLeaves(prevServer, prevDraft).filter((e) => !ignored(e.path));
      // edits win — except where fresh already subsumes them (verbatim value,
      // or a landed instance-list shape change: the server's enriched cards
      // are canonical, never the draft's partial scaffold)
      next = applyEditsOnto(fresh, edits);
    }
    setServer(fresh);
    setDraft(next);
    setOrder(devTypesArr.map((d) => d.name));
  };

  const setField = (path, value) => setDraft((d) => setIn(d, path, value));

  const diff = useMemo(
    () => (server && draft ? diffLeaves(server, draft).filter((e) => !ignored(e.path)) : []),
    [server, draft]
  );

  const errors = useMemo(() => {
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
    // a PMO listing a repo name with no card is refused by the server —
    // surface it inline (removals normally cascade; this catches stragglers)
    const repoNames = new Set((draft.cfg.repos || []).map((r) => r.name));
    (draft.cfg.pmos || []).forEach((p, i) => {
      for (const [field, label] of
           [["repos", "work repo"], ["reference_repos", "reference repo"]]) {
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
  }, [draft]);

  return {
    server, draft, order,
    loaded: !!draft,
    load, rebase, setField,
    diff,
    dirty: diff.length > 0,
    errors,
    discard: () => setDraft(serverRef.current),
  };
}
