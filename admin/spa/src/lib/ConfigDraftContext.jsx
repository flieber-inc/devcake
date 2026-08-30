import React, { createContext, useContext, useEffect, useRef, useState } from "react";
import { get } from "../api.js";
import useConfigDraft from "./useConfigDraft.js";
import { makeReqSeq } from "./reqSeq.js";

// ONE unified draft over {cfg, devTypes, assignments}, shared by
// Connections (Repos / PMO / Skill sources), Fleet, and Settings
// (v0.1.1 B4 + 2026-08-02 + CAKE-159): switching between them keeps the
// draft; the DirtyBar / SaveReviewDialog / NavGuard live once, in
// DraftChrome. Mounted at App level so the provider outlives page switches.
const Ctx = createContext(null);

export function ConfigDraftProvider({ children, health }) {
  const dr = useConfigDraft();
  const [harnesses, setHarnesses] = useState({});
  const [healthInfo, setHealthInfo] = useState(null);
  const [loadErr, setLoadErr] = useState("");
  const firstLoad = useRef(true);
  const reloadSeq = useRef(makeReqSeq());
  // "new card" name tracking for PMO and repo cards lives HERE (audit D5
  // #12, extended): pages unmount on navigation, and a card added this
  // session must keep its editable-name status when the operator returns —
  // an internal Set would be lost and the card born name-locked.
  const pmoNewNamesState = useState(() => new Set());
  const repoNewNamesState = useState(() => new Set());
  // skill sources live under Connections → Skill sources (page unmounts on
  // switch); same D5 #12 survival rule as PMO/repo name tracking
  const skillSourceNewNamesState = useState(() => new Set());

  const reload = async () => {
    // stale-response guard: reload() fires from six places; two overlapping
    // reloads resolving out of order used to rebase() the OLDER snapshot last
    // — rolling the server baseline backward, and since config does not poll,
    // the phantom dirt persisted until the next action (2026-08-12 audit)
    const mine = reloadSeq.current.next();
    const [c, d, a, hs, h] = await Promise.all([
      get("/config"), get("/dev-types"), get("/assignments"),
      get("/harnesses"), get("/health").catch(() => null),
    ]);
    if (!reloadSeq.current.isLatest(mine)) return;   // a newer reload won
    setHarnesses(hs);
    setHealthInfo(h);
    if (firstLoad.current) { dr.load(c, d, a); firstLoad.current = false; }
    else dr.rebase(c, d, a);
  };
  useEffect(() => { reload().catch((e) => setLoadErr(String(e))); }, []);

  // App's 10s /health poll carries bake_status; prefer it so the editor
  // can say baking → ready without a Save/reload.
  const liveHealth = (health && health.harness_pins) ? health : healthInfo;
  return (
    <Ctx.Provider value={{ dr, reload, harnesses, healthInfo: liveHealth, loadErr,
                           pmoNewNamesState, repoNewNamesState,
                           skillSourceNewNamesState }}>
      {children}
    </Ctx.Provider>
  );
}

export function useSharedDraft() {
  return useContext(Ctx);
}
