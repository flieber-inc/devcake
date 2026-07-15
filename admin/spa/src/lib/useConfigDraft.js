import { useMemo, useRef, useState } from "react";
import { diffLeaves, setIn, containerExists } from "./objectPath.js";
import { INSTANCE_NAME_RE, INSTANCE_NAME_RULE } from "./instanceNames.js";

// Paths that never dirty the draft: server-owned state that immediate
// actions (sidebar intake switch, alert dismissal, credential uploads)
// change underneath an open draft.
const IGNORED = [
  /^cfg\.intake_paused$/,
  /^cfg\.dismissed_alerts$/,
  /^cfg\.schema_version$/,
  /^devTypes\.[^.]+\.secrets_present$/,
];
const ignored = (path) => IGNORED.some((re) => re.test(path));

function normalize(cfg, devTypesArr, assignments) {
  return {
    cfg,
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
      for (const e of edits) {
        if (containerExists(fresh, e.path)) next = setIn(next, e.path, e.new);
      }
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
    const rm = draft.cfg.relations_mapper || {};
    if (rm.enabled && !rm.dev_type)
      errs["cfg.relations_mapper.enabled"] =
        "Relations Mapper: periodic service is ON but no Dev Type is selected";
    for (const [mt, a] of Object.entries(draft.assignments || {})) {
      if (a?.dev_type && !draft.devTypes[a.dev_type])
        errs[`assignments.${mt}.dev_type`] =
          `${mt} is assigned to "${a.dev_type}", which no longer exists`;
    }
    for (const [name, dt] of Object.entries(draft.devTypes || {})) {
      if (!Number.isFinite(dt.max_concurrency) || dt.max_concurrency < 1)
        errs[`devTypes.${name}.max_concurrency`] =
          `${name}: max concurrency must be ≥ 1`;
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
