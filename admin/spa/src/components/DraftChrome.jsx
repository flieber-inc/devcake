import React, { useEffect, useState } from "react";
import { send } from "../api.js";
import DirtyBar from "./DirtyBar.jsx";
import SaveReviewDialog from "./SaveReviewDialog.jsx";
import NavGuardDialog from "./NavGuardDialog.jsx";
import { describeDiff } from "../lib/configLabels.js";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";

// The single home of the unified draft's chrome (v0.1.1 B4): DirtyBar,
// SaveReviewDialog, NavGuardDialog, beforeunload, and doSave — rendered once
// by App while a draft page (Configuration / Repositories) is active, so
// config↔repos switches keep the draft and there is exactly ONE nav-guard
// registration.
const DEFAULT_REPO_RE = /^cfg\.pmos\.\d+\.repos$/;

export default function DraftChrome({ registerNavGuard, health }) {
  const { dr, reload } = useSharedDraft();
  const [review, setReview] = useState(null);        // {fromNav?: resolveFn}
  const [saveBusy, setSaveBusy] = useState(false);
  const [saveResults, setSaveResults] = useState(null);
  const [savedFlash, setSavedFlash] = useState(false);
  const [navPrompt, setNavPrompt] = useState(null);  // resolveFn while open

  // nav guard: registered while dirty; the promise resolves from the dialog
  useEffect(() => {
    if (!registerNavGuard) return;
    registerNavGuard(
      dr.dirty ? () => new Promise((resolve) => setNavPrompt(() => resolve)) : null
    );
    return () => registerNavGuard(null);
  }, [dr.dirty]);

  // browser refresh / tab close while dirty
  useEffect(() => {
    if (!dr.dirty) return;
    const h = (e) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [dr.dirty]);

  if (!dr.loaded) return null;

  const doSave = async () => {
    setSaveBusy(true);
    const diff = dr.diff;
    const results = {};
    const cfgKeys = [...new Set(diff.filter((x) => x.path.startsWith("cfg."))
      .map((x) => x.path.split(".")[1]))];
    if (cfgKeys.length) {
      const patch = Object.fromEntries(cfgKeys.map((k) => [k, dr.draft.cfg[k]]));
      // Per-PMO intake is owned by PUT /config/pmos/{name}/intake — never by
      // a draft Save. Stripping the key lets the server inherit the live value
      // (see inherit_pmo_intake); leaving a stale false would undo a pause.
      if (Array.isArray(patch.pmos)) {
        patch.pmos = patch.pmos.map((p) => {
          if (!p || typeof p !== "object") return p;
          const { intake_paused: _drop, ...rest } = p;
          return rest;
        });
      }
      try { await send("PUT", "/config", patch); results["config"] = { ok: true }; }
      catch (e) { results["config"] = { ok: false, error: String(e.message || e) }; }
    }
    const dtNames = [...new Set(diff.filter((x) => x.path.startsWith("devTypes."))
      .map((x) => x.path.split(".")[1]))];
    for (const nm of dtNames) {
      try {
        await send("PUT", `/dev-types/${nm}`, dr.draft.devTypes[nm]);
        results[`dev type ${nm}`] = { ok: true };
      } catch (e) {
        results[`dev type ${nm}`] = { ok: false, error: String(e.message || e) };
      }
    }
    if (diff.some((x) => x.path.startsWith("assignments."))) {
      try { await send("PUT", "/assignments", dr.draft.assignments); results["assignments"] = { ok: true }; }
      catch (e) { results["assignments"] = { ok: false, error: String(e.message || e) }; }
    }
    // refetch + rebase: saved edits match the fresh server and vanish from the
    // diff; failed units keep their drafted leaves → the page stays dirty with
    // exactly the failed subset
    try { await reload(); } catch { /* page keeps last state */ }
    setSaveBusy(false);
    const ok = Object.values(results).every((r) => r.ok);
    if (ok) {
      setReview(null);
      setSaveResults(null);
      setSavedFlash(true);
      setTimeout(() => setSavedFlash(false), 2500);
    } else {
      setSaveResults(results);
    }
    return ok;
  };

  const confirmSave = async () => {
    const fromNav = review?.fromNav;
    const ok = await doSave();
    if (fromNav) fromNav(ok); // proceed only on full success
  };

  const closeReview = () => {
    if (review?.fromNav) review.fromNav(false);
    setReview(null);
    setSaveResults(null);
  };

  // the instance repo set changed while runs are in flight (B5, founder
  // decision): sticky-wins-silently backend semantics mean in-flight
  // missions KEEP their repo — warn in the save review so the operator
  // explicitly acknowledges nothing is re-routed. Uses App's live-polled
  // health (≤10 s stale), not the provider's load-time snapshot.
  let rows = describeDiff(dr.diff);
  const inFlight = health?.active_runs || 0;
  if (inFlight > 0) {
    rows = rows.map((r) => DEFAULT_REPO_RE.test(r.path)
      ? {
          ...r,
          warning:
            `${inFlight} run${inFlight > 1 ? "s" : ""} in flight — missions ` +
            `that already resolved a repository keep it; only newly adopted ` +
            `missions use the new default. Nothing is re-routed.`,
        }
      : r);
  }

  return (
    <>
      <DirtyBar
        count={dr.diff.length}
        errors={dr.errors}
        saved={savedFlash}
        onDiscard={dr.discard}
        onSave={() => setReview({})}
      />
      <SaveReviewDialog
        open={!!review}
        rows={rows}
        busy={saveBusy}
        results={saveResults}
        onConfirm={confirmSave}
        onCancel={closeReview}
        onRetry={doSave}
      />
      <NavGuardDialog
        open={!!navPrompt}
        count={dr.diff.length}
        errors={dr.errors}
        onStay={() => { navPrompt(false); setNavPrompt(null); }}
        onDiscard={() => { dr.discard(); navPrompt(true); setNavPrompt(null); }}
        onSave={() => { setReview({ fromNav: navPrompt }); setNavPrompt(null); }}
      />
    </>
  );
}
