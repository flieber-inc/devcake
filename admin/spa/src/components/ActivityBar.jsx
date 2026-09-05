import React, { useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { get } from "../api.js";
import usePoll from "../lib/usePoll.js";
import { summarizeActivity } from "../lib/activity.js";

const STORAGE = "devcake-activity-bar";
const POLL_MS = 5000;

// Discreet, collapsible status bar (docs/11 §0): what the backend is doing
// right now, from GET /api/v1/activity — so "waiting" and "frozen" never
// look the same. One line collapsed; every phase with its duration expanded.
export default function ActivityBar() {
  const [payload, setPayload] = useState(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(() => {
    try { return localStorage.getItem(STORAGE) === "expanded"; } catch { return false; }
  });
  usePoll(() => {
    get("/activity")
      .then((p) => { setPayload(p); setFailed(false); })
      .catch(() => setFailed(true));
  }, POLL_MS);
  const toggle = () => {
    setOpen((o) => {
      try { localStorage.setItem(STORAGE, !o ? "expanded" : "collapsed"); } catch { /* storage may be unavailable */ }
      return !o;
    });
  };
  const s = summarizeActivity(payload);
  const dot = failed ? "bg-neutral-400"
    : s.state === "stalled" ? "bg-amber-500"
      : s.state === "busy" ? "bg-accent-500 animate-pulse"
        : s.state === "waiting" ? "bg-amber-400"
          : "bg-green-500";
  const line = failed ? "activity unavailable — backend not answering" : s.line;
  return (
    <div className="border-t border-neutral-200 bg-white/80 text-xs text-neutral-600 backdrop-blur dark:border-neutral-800 dark:bg-neutral-900/80 dark:text-neutral-300"
      data-testid="activity-bar" data-state={failed ? "unknown" : s.state}>
      <button type="button" onClick={toggle} aria-expanded={open}
        aria-label={open ? "Collapse backend activity" : "Expand backend activity"}
        className="flex w-full items-center gap-2 px-4 py-1 text-left hover:bg-neutral-50 dark:hover:bg-neutral-800/60 sm:px-8">
        <span className={`h-2 w-2 shrink-0 rounded-full ${dot}`} aria-hidden />
        <span className="min-w-0 flex-1 truncate">{line}</span>
        {open ? <ChevronDown size={13} aria-hidden /> : <ChevronUp size={13} aria-hidden />}
      </button>
      {open && !failed && (
        <div className="max-h-48 overflow-y-auto border-t border-neutral-100 px-4 py-2 dark:border-neutral-800 sm:px-8">
          {s.items.length === 0 && s.skips.length === 0 && (
            <p className="text-neutral-500 dark:text-neutral-400">Nothing in flight.</p>
          )}
          <ul className="space-y-0.5">
            {s.items.map((i, n) => (
              <li key={`${i.kind}-${n}`} className={i.overdue ? "text-amber-700 dark:text-amber-300" : ""}>
                <span className="font-medium">{i.label}</span>
                <span className="text-neutral-500 dark:text-neutral-400"> · {i.elapsed}{i.overdue ? " · overdue" : ""}</span>
              </li>
            ))}
            {s.skips.map((k, n) => (
              <li key={`skip-${n}`} className="text-amber-700 dark:text-amber-300">{k.label}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
