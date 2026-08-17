import React from "react";

// Status dot. Preferred API: state ∈ ok | running | broken | unknown
// (the Runs-table color code — green / pulsing accent / red / grey).
// Legacy API: ok = true | false | undefined (sidebar services — no running).
const STATE_CLS = {
  ok: "bg-green-500",
  running: "bg-accent-500 animate-pulse",
  broken: "bg-red-500",
  unknown: "bg-neutral-400",
};

export default function StatusDot({ ok, state, label, title }) {
  const s = state ?? (ok === true ? "ok" : ok === false ? "broken" : "unknown");
  return (
    <span title={title}
      className="flex items-center gap-1.5 text-xs text-neutral-500 dark:text-neutral-400">
      <span className={`inline-block h-2 w-2 rounded-full ${STATE_CLS[s] || STATE_CLS.unknown}`} />
      {label}
    </span>
  );
}
