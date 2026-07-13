import React from "react";

const FAILED = ["failed", "timed_out", "orphaned"];

// verdict: app-level judgment (Phase-1 backend field). A run can be
// state="finished" (the executor succeeded) while the app refused to act on
// its outcome — shown as an amber "finished*".
export default function StatusPill({ state, verdict }) {
  const flagged = state === "finished" && !!verdict;
  const cls = flagged
    ? "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300"
    : state === "finished"
      ? "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300"
      : FAILED.includes(state)
        ? "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300"
        : "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300";
  const dot = flagged
    ? "bg-amber-500"
    : state === "finished"
      ? "bg-green-500"
      : FAILED.includes(state)
        ? "bg-red-500"
        : "bg-blue-500 animate-pulse";
  return (
    <span title={flagged ? verdict : undefined}
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {flagged ? "finished*" : state}
    </span>
  );
}
