import React, { useEffect, useRef, useState } from "react";
import { get, getText, send } from "./../api.js";

const cfg = window.DEVCAKE || {};
const PAGE = 25;

// deep link: OO traces filtered to this run (query is base64 — verified shape)
const traceUrl = (runId) =>
  `${cfg.ooUrl || "http://localhost:5080"}/web/traces?org_identifier=default` +
  `&stream=default&period=1w&search_mode=spans&query=${btoa(`devcake_run_id='${runId}'`)}`;

function ConfirmDialog({ open, title, body, confirmLabel, busy, onConfirm, onCancel }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="w-full max-w-lg rounded-xl bg-white p-6 shadow-xl dark:bg-neutral-900">
        <h4 className="mb-2 text-base font-semibold">{title}</h4>
        <p className="mb-5 whitespace-pre-line text-sm text-neutral-600 dark:text-neutral-300">
          {body}
        </p>
        <div className="flex justify-end gap-2">
          <button
            disabled={busy}
            onClick={onCancel}
            className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium disabled:opacity-40 dark:border-neutral-700"
          >
            Cancel
          </button>
          <button
            disabled={busy}
            onClick={onConfirm}
            className="rounded-md bg-red-700 px-3 py-1.5 text-sm font-medium text-white hover:brightness-110 disabled:opacity-40"
          >
            {busy ? "Clearing…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

const TERMINAL_STATES = ["finished", "failed", "timed_out", "orphaned"];

// Popup terminal: replays the stored run log, then follows live over SSE.
// A simulacrum — the harness runs headless (no PTY), so this shows the
// condensed line feed the Dev relays, not an interactive shell.
function RunTerminal({ run, onClose }) {
  const [lines, setLines] = useState([]);
  const [live, setLive] = useState(!TERMINAL_STATES.includes(run.state));
  const [err, setErr] = useState("");
  const preRef = useRef(null);
  const stickRef = useRef(true);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    let es;
    if (TERMINAL_STATES.includes(run.state)) {
      getText(`/runs/${run.run_id}/log?tail=1000`)
        .then((t) => setLines(t ? t.replace(/\n$/, "").split("\n") : []))
        .catch((e) => setErr(String(e.message || e)));
    } else {
      // server replays the file before following — no separate initial fetch
      es = new EventSource(`/api/v1/runs/${run.run_id}/log/stream`);
      es.onmessage = (e) => setLines((prev) => [...prev.slice(-4999), e.data]);
      es.addEventListener("end", () => {
        setLines((prev) => [...prev, "[process exited]"]);
        setLive(false);
        es.close();
      });
      es.onerror = () => { setLive(false); es.close(); };
    }
    return () => es && es.close();
  }, [run.run_id]);

  useEffect(() => {
    const el = preRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const onScroll = () => {
    const el = preRef.current;
    if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}>
      <div className="flex h-[70vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-neutral-700 bg-neutral-950 shadow-2xl"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 border-b border-neutral-800 bg-neutral-900 px-4 py-2.5">
          <span className="h-3 w-3 rounded-full bg-red-500" />
          <span className="h-3 w-3 rounded-full bg-yellow-500" />
          <span className="h-3 w-3 rounded-full bg-green-500" />
          <span className="ml-3 truncate font-mono text-xs text-neutral-300">
            {run.run_id}
          </span>
          <span className={`rounded px-1.5 py-0.5 text-xs ${
            live ? "bg-blue-900 text-blue-200" : "bg-neutral-800 text-neutral-400"}`}>
            {live ? "live" : run.state}
          </span>
          <span className="grow" />
          <button onClick={onClose} aria-label="Close"
            className="rounded px-2 py-0.5 text-sm text-neutral-400 hover:bg-neutral-800 hover:text-neutral-100">
            ✕
          </button>
        </div>
        <pre ref={preRef} onScroll={onScroll}
          className="grow overflow-y-auto whitespace-pre-wrap break-all p-4 font-mono text-xs leading-relaxed text-neutral-200">
          {err
            ? `error loading log: ${err}`
            : lines.length === 0 && !live
              ? "(no output relayed for this run)"
              : lines.join("\n")}
          {live && <span className="animate-pulse text-neutral-400">{lines.length ? "\n" : ""}▊</span>}
        </pre>
      </div>
    </div>
  );
}

export default function ExecutorTab() {
  const [data, setData] = useState({ total: 0, runs: [] });
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [openRun, setOpenRun] = useState(null);
  const [clearing, setClearing] = useState(false);
  const [clearMsg, setClearMsg] = useState("");
  const [clearErr, setClearErr] = useState("");

  const load = () => {
    const q = `/runs?limit=${PAGE}&offset=${offset}` +
      (filter ? `&mission_key=${encodeURIComponent(filter)}` : "");
    return get(q).then(setData).catch(() => {});
  };

  useEffect(() => {
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [offset, filter]);

  const doClear = async () => {
    setClearing(true);
    setClearErr("");
    setClearMsg("");
    try {
      const result = await send("POST", "/system/clear-runs");
      const local = result.local?.runs_deleted ?? 0;
      const dagu = result.dagu?.deleted ?? 0;
      const oo = (result.openobserve?.deleted || []).length;
      setClearMsg(
        `Cleared ${local} local run${local === 1 ? "" : "s"}, ` +
        `${dagu} Dagu run${dagu === 1 ? "" : "s"}, ` +
        `${oo} OpenObserve stream${oo === 1 ? "" : "s"}. ` +
        `Config and credentials preserved.`
      );
      if (!result.ok) {
        const bits = [];
        if (result.dagu?.failed?.length) bits.push(`Dagu still holds: ${result.dagu.failed.join(", ")}`);
        if (result.openobserve?.errors?.length) bits.push(result.openobserve.errors.join("; "));
        if (result.dagu?.error) bits.push(result.dagu.error);
        if (result.openobserve?.error) bits.push(result.openobserve.error);
        if (result.redis?.error) bits.push(result.redis.error);
        if (bits.length) setClearErr(bits.join(" · "));
      }
      setOffset(0);
      setFilter("");
      await load();
      setConfirmOpen(false);
    } catch (e) {
      setClearErr(String(e.message || e));
    } finally {
      setClearing(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">Executor — Dagu</h2>
        <div className="flex items-center gap-2">
          <button
            onClick={() => { setConfirmOpen(true); setClearErr(""); }}
            className="rounded-lg border border-red-300 px-4 py-2 text-sm font-semibold text-red-700 hover:bg-red-50 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-950"
          >
            Clear runs
          </button>
          <a href={cfg.daguUrl || "http://localhost:8525"} target="_blank" rel="noopener"
            className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white hover:brightness-110">
            Open Dagu ↗
          </a>
        </div>
      </div>
      {clearMsg && (
        <p className="rounded-md border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-800 dark:border-green-900 dark:bg-green-950 dark:text-green-200">
          {clearMsg}
        </p>
      )}
      {clearErr && (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
          Partial clear: {clearErr}
        </p>
      )}
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
            <tr key={r.run_id} onClick={() => setOpenRun(r)}
              title="Click to open the run terminal"
              className="cursor-pointer border-t border-neutral-100 hover:bg-neutral-50 dark:border-neutral-800 dark:hover:bg-neutral-900">
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
                  onClick={(e) => e.stopPropagation()}
                  target="_blank" rel="noopener">trace ↗</a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {openRun && <RunTerminal run={openRun} onClose={() => setOpenRun(null)} />}
      <ConfirmDialog
        open={confirmOpen}
        title="Clear all run history?"
        body={
          "This wipes local run records, stops any in-flight Devs, deletes Dagu " +
          "execution history, and empties OpenObserve logs and traces.\n\n" +
          "Config, credentials, and everything in Linear / GitHub / GitLab are " +
          "untouched. Attempt counters reset (INV-1).\n\n" +
          "This cannot be undone."
        }
        confirmLabel="Clear everything"
        busy={clearing}
        onConfirm={doClear}
        onCancel={() => !clearing && setConfirmOpen(false)}
      />
    </div>
  );
}
