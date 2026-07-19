import React, { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, ExternalLink, SquareTerminal } from "lucide-react";
import { get, send } from "../api.js";
import PageHeader from "../components/PageHeader.jsx";
import { Card } from "../components/Card.jsx";
import Button from "../components/Button.jsx";
import MoreMenu from "../components/MoreMenu.jsx";
import StatusPill from "../components/StatusPill.jsx";
import RunTerminal from "../components/RunTerminal.jsx";
import StageGlyph from "../components/StageGlyph.jsx";
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
  const [query, setQuery] = useState("");   // debounced filter — one fetch per pause, not per keystroke
  useEffect(() => {
    const t = setTimeout(() => setQuery(filter), 300);
    return () => clearTimeout(t);
  }, [filter]);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [openRun, setOpenRun] = useState(null);
  const [clearing, setClearing] = useState(false);
  const [stopConfirmOpen, setStopConfirmOpen] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [clearMsg, setClearMsg] = useState("");
  const [clearErr, setClearErr] = useState("");

  const load = () => {
    const q = `/runs?limit=${PAGE}&offset=${offset}` +
      (query ? `&mission_key=${encodeURIComponent(query)}` : "");
    return get(q).then(setData).catch(() => {});
  };

  usePoll(load, 10000, [offset, query]);

  const doClear = async () => {
    setClearing(true);
    setClearErr("");
    setClearMsg("");
    try {
      const result = await send("POST", "/system/clear-runs");
      const local = result.local?.runs_deleted ?? 0;
      const dagu = result.dagu?.deleted ?? 0;
      const oo = (result.openobserve?.deleted || []).length;
      const act = result.activity_repos?.deleted ?? 0;
      setClearMsg(
        `Cleared ${local} local run${local === 1 ? "" : "s"}, ` +
        `${dagu} Dagu run${dagu === 1 ? "" : "s"}, ` +
        `${oo} OpenObserve stream${oo === 1 ? "" : "s"}, ` +
        `${act} activity repo${act === 1 ? "" : "s"}. ` +
        `Config and credentials preserved.`
      );
      if (!result.ok) {
        const bits = [];
        // drain phase (#28 stop-and-drain): a container wedged past the cap
        // means the wipe ran while it was still live — surface it loudly
        if (result.stopped?.undrained?.length) {
          const force = result.stopped.force_remove_attempted
            ? " (Dagu force-stop attempted; app has no docker.sock)"
            : "";
          bits.push(`Still running after drain${force}: ${result.stopped.undrained.join(", ")}`);
        }
        if (result.stopped?.error) bits.push(`Stop phase: ${result.stopped.error}`);
        if (result.dagu?.failed?.length) bits.push(`Dagu still holds: ${result.dagu.failed.join(", ")}`);
        if (result.openobserve?.errors?.length) bits.push(result.openobserve.errors.join("; "));
        if (result.dagu?.error) bits.push(result.dagu.error);
        if (result.openobserve?.error) bits.push(result.openobserve.error);
        if (result.redis?.error) bits.push(result.redis.error);
        if (result.activity_repos?.errors?.length) bits.push(result.activity_repos.errors.join("; "));
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

  const [stopErr, setStopErr] = useState("");
  const doStopAll = async () => {
    setStopping(true);
    setStopErr("");
    setClearMsg("");
    try {
      const result = await send("POST", "/system/stop-runs");
      const n = (result.stopped || []).length;
      const fin = (result.skipped_finalizing || []).length;
      const errs = result.errors || [];
      setClearMsg(
        `Stopped ${n} run${n === 1 ? "" : "s"}.` +
        (fin ? ` ${fin} finalizing run${fin === 1 ? "" : "s"} left to complete on ${fin === 1 ? "its" : "their"} own.` : "")
      );
      if (errs.length) {
        // per-run kill failures: report them, keep the dialog open so the
        // operator can retry (nothing was deleted — this is stop, not clear)
        setStopErr(errs.map((e) => `${e.run_id}: ${e.error}`).join(" · "));
      } else {
        setStopConfirmOpen(false);
      }
      load();
    } catch (e) {
      setStopErr(String(e.message || e));
    } finally {
      setStopping(false);
    }
  };

  return (
    <div className="space-y-4">
      <PageHeader title="Runs" subtitle="Dev runs executed by Dagu — click a row for its terminal"
        actions={
          <>
            <a href={cfg.daguUrl || "http://localhost:8525"} target="_blank" rel="noopener"
              className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white transition hover:bg-accent-700">
              Open Dagu <ExternalLink size={13} aria-hidden />
            </a>
            <MoreMenu label="More run actions" items={[
              { label: "Stop all runs", danger: true,
                desc: "Kills every in-flight Dev (each counts as a failed attempt). Finalizing runs complete on their own.",
                onClick: () => { setStopConfirmOpen(true); setClearErr(""); } },
              { label: "Clear run history", danger: true,
                desc: "Wipes local records, Dagu history and OpenObserve data. Cannot be undone.",
                onClick: () => { setConfirmOpen(true); setClearErr(""); } },
            ]} />
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
      {stopErr && (
        <p className="rounded-card border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
          Stop failed (nothing was deleted): {stopErr}
        </p>
      )}
      <Card className="p-4">
        <div className="mb-3 flex items-center gap-3">
          <span className="relative">
            <Input className="w-64 pr-7"
              placeholder="Filter by mission (e.g. DEV-17)"
              aria-label="Filter runs by mission key"
              value={filter}
              onChange={(e) => { setFilter(e.target.value); setOffset(0); }}
            />
            {filter && (
              <button type="button" aria-label="Clear filter"
                onClick={() => { setFilter(""); setOffset(0); }}
                className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-neutral-500 hover:text-neutral-800 dark:text-neutral-400 dark:hover:text-neutral-100">
                ✕
              </button>
            )}
          </span>
          <span className="text-xs text-neutral-500 dark:text-neutral-400">{data.total} runs</span>
          <span className="grow" />
          <Button kind="ghost" size="sm" icon={ChevronLeft} disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE))}>newer</Button>
          <span className="text-xs tabular-nums text-neutral-500 dark:text-neutral-400">
            {data.total === 0 ? "0" : `${offset + 1}–${Math.min(offset + PAGE, data.total)}`} of {data.total}
          </span>
          <Button kind="ghost" size="sm" disabled={offset + PAGE >= data.total}
            onClick={() => setOffset(offset + PAGE)}>
            older <ChevronRight size={13} aria-hidden />
          </Button>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[42rem] text-left text-sm">
            <thead className="text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
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
                <tr><td colSpan={6} className="py-6 text-center text-sm text-neutral-500 dark:text-neutral-400">
                  No runs{filter ? " match this filter" : " yet"}.
                </td></tr>
              )}
              {data.runs.map((r) => (
                <tr key={r.run_id} onClick={() => setOpenRun(r)}
                  title="Click to open the run terminal"
                  className="cursor-pointer border-t border-neutral-100 hover:bg-stone-50 dark:border-neutral-800 dark:hover:bg-neutral-900">
                  <td className="max-w-[18rem] py-2 pr-3">
                    <span className="flex items-center gap-2">
                      {r.mission_type && <StageGlyph stage={r.mission_type} />}
                      <button type="button"
                        onClick={(e) => { e.stopPropagation(); setOpenRun(r); }}
                        title="Open the run terminal"
                        className="inline-flex items-center gap-1.5 rounded font-mono text-xs underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60">
                        <SquareTerminal size={12} className="shrink-0 text-neutral-500 dark:text-neutral-400" aria-hidden />
                        {r.run_id}
                      </button>
                    </span>
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
                    ) : <span className="text-xs text-neutral-500 dark:text-neutral-400">—</span>}
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
        open={stopConfirmOpen}
        title="Stop all in-flight runs?"
        body={
          "Every dispatched or running Dev is killed and its attempt counts " +
          "as failed (the missions stay on the board and reschedule normally). " +
          "Runs already finalizing are left to complete their bookkeeping.\n\n" +
          "Nothing is deleted \u2014 use Clear run history for that."
        }
        confirmLabel="Stop all runs"
        busy={stopping}
        error={stopErr}
        onConfirm={doStopAll}
        onCancel={() => !stopping && (setStopConfirmOpen(false), setStopErr(""))}
      />
      <ConfirmDialog
        open={confirmOpen}
        title="Clear all run history?"
        body={
          "This wipes local run records, stops any in-flight Devs, deletes Dagu " +
          "execution history, and empties OpenObserve logs and traces. " +
          "Per-mission activity repos on the internal Gitea are deleted too — " +
          "their git history includes pre-edit feed states your PMO no longer " +
          "shows.\n\n" +
          "Config, credentials, operator repos, the skill-store, work repos, " +
          "and everything in your PMO are untouched. Every mission's retry " +
          "count starts fresh.\n\n" +
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
