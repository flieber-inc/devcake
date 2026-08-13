import { useMemo, useRef, useState } from "react";
import { diffLeaves, setIn, applyEditsOnto } from "./objectPath.js";
import { draftErrors } from "./draftErrors.js";

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

  const errors = useMemo(() => draftErrors(draft), [draft]);

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

