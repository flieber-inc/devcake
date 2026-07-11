import React from "react";

const cfg = window.DEVCAKE || {};
const oo = cfg.ooUrl || "http://localhost:5080";

export default function LogsTab() {
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Logs — OpenObserve</h2>
        <a
          href={oo}
          target="_blank"
          rel="noopener"
          className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white hover:brightness-110"
        >
          Open OpenObserve ↗
        </a>
      </div>
      <p className="text-sm text-neutral-500">
        Every Dev run is one trace (dispatch → container → finalization). Token counts and cost
        ride each <code>dev.run</code> span as <code>devcake.*</code> attributes; each step also
        posts a human-readable token report to the mission's activity feed.
      </p>
      <ul className="list-inside list-disc text-sm text-neutral-600 dark:text-neutral-300">
        <li>Traces live in the <code>default</code> trace stream.</li>
        <li>Container stdout (dagu, redis) lands in the <code>container_logs</code> log stream.</li>
        <li>Search a run by its id, e.g. <code>DEV-17-3-EXECUTE-560E6T</code>.</li>
      </ul>
    </div>
  );
}
