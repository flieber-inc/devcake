import React, { createContext, useContext, useEffect, useRef, useState } from "react";
import { get } from "../api.js";
import useConfigDraft from "./useConfigDraft.js";

// ONE unified draft over {cfg, devTypes, assignments}, shared by the
// Configuration, Repositories and PMO pages (v0.1.1 B4 + 2026-08-02 nav
// reorg): switching between them keeps the draft; the DirtyBar /
// SaveReviewDialog / NavGuard live once, in DraftChrome. Mounted at App
// level so the provider outlives page switches.
const Ctx = createContext(null);

export function ConfigDraftProvider({ children }) {
  const dr = useConfigDraft();
  const [harnesses, setHarnesses] = useState({});
  const [healthInfo, setHealthInfo] = useState(null);
  const [loadErr, setLoadErr] = useState("");
  const firstLoad = useRef(true);
  // "new card" name tracking for PMO and repo cards lives HERE (audit D5
  // #12, extended): pages unmount on navigation, and a card added this
  // session must keep its editable-name status when the operator returns —
  // an internal Set would be lost and the card born name-locked.
  const pmoNewNamesState = useState(() => new Set());
  const repoNewNamesState = useState(() => new Set());

  const reload = async () => {
    const [c, d, a, hs, h] = await Promise.all([
      get("/config"), get("/dev-types"), get("/assignments"),
      get("/harnesses"), get("/health").catch(() => null),
    ]);
    setHarnesses(hs);
    setHealthInfo(h);
    if (firstLoad.current) { dr.load(c, d, a); firstLoad.current = false; }
    else dr.rebase(c, d, a);
  };
  useEffect(() => { reload().catch((e) => setLoadErr(String(e))); }, []);

  return (
    <Ctx.Provider value={{ dr, reload, harnesses, healthInfo, loadErr,
                           pmoNewNamesState, repoNewNamesState }}>
      {children}
    </Ctx.Provider>
  );
}

export function useSharedDraft() {
  return useContext(Ctx);
}
