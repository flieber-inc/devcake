import React, { useState } from "react";
import { ChevronLeft, ChevronRight, ExternalLink, Trash2 } from "lucide-react";
import { get, send } from "../api.js";
import PageHeader from "../components/PageHeader.jsx";
import { Card } from "../components/Card.jsx";
import Button from "../components/Button.jsx";
import StatusPill from "../components/StatusPill.jsx";
import RunTerminal from "../components/RunTerminal.jsx";
import { ConfirmDialog } from "../components/Modal.jsx";
import { Input } from "../components/Field.jsx";
import usePoll from "../lib/usePoll.js";
import { relTime, fullTime, duration } from "../lib/format.js";

const cfg = window.DEVCAKE || {};
const PAGE = 25;

// deep link: OO traces filtered to this run (query is base64 — verified shape)
const traceUrl = (runId) =>
  `${cfg.ooUrl || "http://localhost:5080"}/web/traces?org_identifier=default` +
  `&stream=default&period=1w&search_mode=spans&query=${btoa(`devcake_run_id='${runId}'`)}`;

export default function RunsPage() {
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

  usePoll(load, 10000, [offset, filter]);

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
      <PageHeader title="Runs" subtitle="Dev runs executed by Dagu — click a row for its terminal"
        actions={
          <>
            <Button kind="danger-ghost" icon={Trash2}
              onClick={() => { setConfirmOpen(true); setClearErr(""); }}>
              Clear runs
            </Button>
            <a href={cfg.daguUrl || "http://localhost:8525"} target="_blank" rel="noopener"
              className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white transition hover:bg-accent-700">
              Open Dagu <ExternalLink size={13} aria-hidden />
            </a>
          </>
        } />
      {clearMsg && (
        <p className="rounded-card border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-800 dark:border-green-900 dark:bg-green-950 dark:text-green-200">
          {clearMsg}
        </p>
      )}
      {clearErr && (
        <p className="rounded-card border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
          Partial clear: {clearErr}
        </p>
      )}
      <Card className="p-4">
        <div className="mb-3 flex items-center gap-3">
          <Input className="w-64"
            placeholder="Filter by mission (e.g. DEV-17)"
            value={filter}
            onChange={(e) => { setFilter(e.target.value); setOffset(0); }}
          />
          <span className="text-xs text-neutral-400">{data.total} runs</span>
          <span className="grow" />
          <Button kind="ghost" size="sm" icon={ChevronLeft} disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE))}>newer</Button>
          <span className="text-xs tabular-nums text-neutral-400">
            {data.total === 0 ? "0" : `${offset + 1}–${Math.min(offset + PAGE, data.total)}`} of {data.total}
          </span>
          <Button kind="ghost" size="sm" disabled={offset + PAGE >= data.total}
            onClick={() => setOffset(offset + PAGE)}>
            older <ChevronRight size={13} aria-hidden />
          </Button>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[42rem] text-left text-sm">
            <thead className="text-xs uppercase tracking-wide text-neutral-400">
              <tr>
                <th className="py-1.5 pr-3">run</th>
                <th className="pr-3">mission</th>
                <th className="pr-3">state</th>
                <th className="pr-3">started</th>
                <th className="pr-3">duration</th>
                <th>trace</th>
              </tr>
            </thead>
            <tbody>
              {data.runs.length === 0 && (
                <tr><td colSpan={6} className="py-6 text-center text-sm text-neutral-400">
                  No runs{filter ? " match this filter" : " yet"}.
                </td></tr>
              )}
              {data.runs.map((r) => (
                <tr key={r.run_id} onClick={() => setOpenRun(r)}
                  title="Click to open the run terminal"
                  className="cursor-pointer border-t border-neutral-100 hover:bg-stone-50 dark:border-neutral-800 dark:hover:bg-neutral-900">
                  <td className="max-w-[18rem] py-2 pr-3">
                    <span className="font-mono text-xs">{r.run_id}</span>
                    {(r.error || r.verdict) && (
                      <span
                        title={r.error || r.verdict}
                        className={`block truncate text-[11px] ${
                          r.error
                            ? "text-red-600 dark:text-red-400"
                            : "text-amber-600 dark:text-amber-400"
                        }`}
                      >
                        {r.error || r.verdict}
                      </span>
                    )}
                  </td>
                  <td className="pr-3">
                    {r.mission_key ? (
                      <span className="inline-flex items-center gap-1.5">
                        <span className="font-mono text-xs">{r.mission_key}</span>
                        {r.mission_type && (
                          <span className="rounded bg-stone-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-neutral-500 dark:bg-neutral-800 dark:text-neutral-400">
                            {r.mission_type}
                          </span>
                        )}
                      </span>
                    ) : <span className="text-xs text-neutral-400">—</span>}
                  </td>
                  <td className="pr-3"><StatusPill state={r.state} verdict={r.verdict} /></td>
                  <td className="whitespace-nowrap pr-3 text-xs text-neutral-500 dark:text-neutral-400"
                    title={fullTime(r.started_at)}>
                    {relTime(r.started_at)}
                  </td>
                  <td className="whitespace-nowrap pr-3 text-xs tabular-nums text-neutral-500 dark:text-neutral-400">
                    {duration(r.started_at, r.ended_at)}
                  </td>
                  <td>
                    <a className="inline-flex items-center gap-0.5 text-xs text-accent-700 underline underline-offset-2 dark:text-accent-300"
                      href={traceUrl(r.run_id)}
                      onClick={(e) => e.stopPropagation()}
                      target="_blank" rel="noopener">
                      trace <ExternalLink size={10} aria-hidden />
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      {openRun && <RunTerminal run={openRun} onClose={() => setOpenRun(null)} />}
      <ConfirmDialog
        open={confirmOpen}
        title="Clear all run history?"
        body={
          "This wipes local run records, stops any in-flight Devs, deletes Dagu " +
          "execution history, and empties OpenObserve logs and traces.\n\n" +
          "Config, credentials, and everything in your PMO and forge are " +
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
