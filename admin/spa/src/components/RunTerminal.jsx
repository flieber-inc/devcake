import React, { useEffect, useRef, useState } from "react";
import { Copy, Check } from "lucide-react";
import { Overlay } from "./Modal.jsx";
import { getText } from "../api.js";

export const TERMINAL_STATES = ["finished", "failed", "timed_out", "orphaned"];

// Popup terminal: replays the stored run log, then follows live over SSE.
// A simulacrum — the harness runs headless (no PTY), so this shows the
// condensed line feed the Dev relays, not an interactive shell.
export default function RunTerminal({ run, onClose }) {
  const [lines, setLines] = useState([]);
  const [live, setLive] = useState(!TERMINAL_STATES.includes(run.state));
  const [err, setErr] = useState("");
  const [copied, setCopied] = useState(false);
  const preRef = useRef(null);
  const stickRef = useRef(true);

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

  const copy = () => {
    navigator.clipboard?.writeText(lines.join("\n")).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  const note = run.error || run.verdict; // app-level judgment, when present

  return (
    // Overlay supplies the focus trap, Esc/backdrop close, <main> scroll
    // lock and focus restore; the terminal keeps its always-dark chrome.
    <Overlay onDismiss={onClose}
      ariaLabel={`Run terminal ${run.run_id}`}
      surfaceClass="border border-neutral-700 bg-neutral-950"
      className="flex h-[75vh] max-w-5xl flex-col overflow-hidden">
        <div className="flex items-center gap-2 border-b border-neutral-800 bg-neutral-900 px-4 py-2.5">
          <span className="h-3 w-3 rounded-full bg-red-500" />
          <span className="h-3 w-3 rounded-full bg-amber-400" />
          <span className="h-3 w-3 rounded-full bg-green-500" />
          <span className="ml-3 truncate font-mono text-xs text-neutral-300">
            {run.run_id}
          </span>
          <span className={`rounded px-1.5 py-0.5 text-xs ${
            live ? "bg-accent-900 text-accent-200" : "bg-neutral-800 text-neutral-400"}`}>
            {live ? "live" : run.state}
          </span>
          {(run.memory_mounts || []).length > 0 && (
            <span className="max-w-[22rem] truncate font-mono text-[10px] text-neutral-400"
              title={(run.memory_mounts || []).map((m) =>
                `${m.card} (${m.binding}${m.stale_cache ? ", stale" : ""}) @ ${(m.commit || "").slice(0, 8)}`
              ).join(" · ")}>
              memory: {(run.memory_mounts || []).map((m) =>
                `${m.card}${m.stale_cache ? "*" : ""}`).join(", ")}
            </span>
          )}
          {note && !live && (
            <span title={note}
              className="max-w-[18rem] truncate rounded bg-amber-950 px-1.5 py-0.5 text-xs text-amber-300">
              {note}
            </span>
          )}
          <span className="grow" />
          <button onClick={copy} aria-label="Copy log"
            title="Copy log to clipboard"
            className="rounded p-1.5 text-neutral-400 hover:bg-neutral-800 hover:text-neutral-100">
            {copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}
          </button>
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
    </Overlay>
  );
}
