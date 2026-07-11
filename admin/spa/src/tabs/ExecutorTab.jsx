import React, { useEffect, useState } from "react";
import { get } from "./../api.js";

const cfg = window.DEVCAKE || {};

export default function ExecutorTab() {
  const [runs, setRuns] = useState([]);
  useEffect(() => {
    const load = () => get("/runs?limit=25").then(setRuns).catch(() => {});
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Executor — Dagu</h2>
        <a
          href={cfg.daguUrl || "http://localhost:8525"}
          target="_blank"
          rel="noopener"
          className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white hover:brightness-110"
        >
          Open Dagu ↗
        </a>
      </div>
      <p className="text-sm text-neutral-500">
        Every run is named <code>{"{mission}-{step}-{type}-{id}"}</code> — the same name in Dagu,
        docker, traces, and this table.
      </p>
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-neutral-400">
          <tr>
            <th className="py-1.5">run</th>
            <th>state</th>
            <th>started</th>
            <th>ended</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.run_id} className="border-t border-neutral-100 dark:border-neutral-800">
              <td className="py-1.5 font-mono text-xs">{r.run_id}</td>
              <td>
                <span
                  className={`rounded px-1.5 py-0.5 text-xs ${
                    r.state === "finished"
                      ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200"
                      : ["failed", "timed_out", "orphaned"].includes(r.state)
                        ? "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200"
                        : "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200"
                  }`}
                >
                  {r.state}
                </span>
              </td>
              <td className="text-xs">{r.started_at?.slice(11, 19) || "—"}</td>
              <td className="text-xs">{r.ended_at?.slice(11, 19) || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
