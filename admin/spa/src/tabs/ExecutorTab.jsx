import React, { useEffect, useState } from "react";
import { get } from "./../api.js";

const cfg = window.DEVCAKE || {};
const PAGE = 25;

// deep link: OO traces filtered to this run (query is base64 — verified shape)
const traceUrl = (runId) =>
  `${cfg.ooUrl || "http://localhost:5080"}/web/traces?org_identifier=default` +
  `&stream=default&period=1w&search_mode=spans&query=${btoa(`devcake_run_id='${runId}'`)}`;

export default function ExecutorTab() {
  const [data, setData] = useState({ total: 0, runs: [] });
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState("");
  useEffect(() => {
    const q = `/runs?limit=${PAGE}&offset=${offset}` +
      (filter ? `&mission_key=${encodeURIComponent(filter)}` : "");
    const load = () => get(q).then(setData).catch(() => {});
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [offset, filter]);
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Executor — Dagu</h2>
        <a href={cfg.daguUrl || "http://localhost:8525"} target="_blank" rel="noopener"
          className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white hover:brightness-110">
          Open Dagu ↗
        </a>
      </div>
      <div className="flex items-center gap-3">
        <input
          className="w-64 rounded-md border border-neutral-300 bg-white px-2.5 py-1.5 text-sm dark:border-neutral-700 dark:bg-neutral-950"
          placeholder="Filter by mission (e.g. DEV-17)"
          value={filter}
          onChange={(e) => { setFilter(e.target.value); setOffset(0); }}
        />
        <span className="text-xs text-neutral-400">{data.total} runs</span>
        <span className="grow" />
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}
          className="rounded border border-neutral-300 px-2 py-1 text-xs disabled:opacity-30 dark:border-neutral-700">← newer</button>
        <span className="text-xs text-neutral-400">
          {data.total === 0 ? "0" : `${offset + 1}–${Math.min(offset + PAGE, data.total)}`} of {data.total}
        </span>
        <button disabled={offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}
          className="rounded border border-neutral-300 px-2 py-1 text-xs disabled:opacity-30 dark:border-neutral-700">older →</button>
      </div>
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-neutral-400">
          <tr><th className="py-1.5">run</th><th>state</th><th>started</th><th>ended</th><th>trace</th></tr>
        </thead>
        <tbody>
          {data.runs.map((r) => (
            <tr key={r.run_id} className="border-t border-neutral-100 dark:border-neutral-800">
              <td className="py-1.5 font-mono text-xs">{r.run_id}</td>
              <td>
                <span className={`rounded px-1.5 py-0.5 text-xs ${
                  r.state === "finished"
                    ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200"
                    : ["failed", "timed_out", "orphaned"].includes(r.state)
                      ? "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200"
                      : "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200"}`}>
                  {r.state}
                </span>
              </td>
              <td className="text-xs">{r.started_at?.slice(11, 19) || "—"}</td>
              <td className="text-xs">{r.ended_at?.slice(11, 19) || "—"}</td>
              <td>
                <a className="text-xs text-accent underline" href={traceUrl(r.run_id)}
                  target="_blank" rel="noopener">trace ↗</a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
