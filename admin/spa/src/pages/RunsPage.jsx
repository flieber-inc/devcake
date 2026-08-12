import React, { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, ExternalLink, SquareTerminal } from "lucide-react";
import { get, send } from "../api.js";
import { makeReqSeq } from "../lib/reqSeq.js";
import PageHeader from "../components/PageHeader.jsx";
import { Card } from "../components/Card.jsx";
import Button from "../components/Button.jsx";
import MoreMenu from "../components/MoreMenu.jsx";
import StatusPill from "../components/StatusPill.jsx";
import RunTerminal from "../components/RunTerminal.jsx";
import StageGlyph from "../components/StageGlyph.jsx";
import { ConfirmDialog } from "../components/Modal.jsx";
import { Input, Select } from "../components/Field.jsx";
import CostInputsModal from "../components/CostInputsModal.jsx";
import usePoll from "../lib/usePoll.js";
import { relTime, fullTime, duration, durationSeconds, tokens, usd } from "../lib/format.js";

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
  // per-run stop (2026-08 reviewer round): the MissionDrawer's Stop, lifted
  // onto the table — same endpoint, same eligibility, same copy
  const [stopTarget, setStopTarget] = useState(null);
  const [stopOneBusy, setStopOneBusy] = useState(false);
  const [stopOneErr, setStopOneErr] = useState("");
  const [clearMsg, setClearMsg] = useState("");
  const [clearErr, setClearErr] = useState("");
  const [pmoRef, setPmoRef] = useState("");
  const [fromDate, setFromDate] = useState("");   // UTC calendar days —
  const [toDate, setToDate] = useState("");       // run timestamps are UTC
  const [costOpen, setCostOpen] = useState(false);
  // server-side sort over the WHOLE filtered set (a client-side sort would
  // only reorder the visible page); "started"/desc is the server default
  const [sortKey, setSortKey] = useState("started");
  const [sortDir, setSortDir] = useState("desc");
  // aggregate-by-mission: pagination flips to missions; the active sort
  // orders GROUPS by their aggregate (founder decision 2026-08-02)
  const [grouped, setGrouped] = useState(false);

  // stale-response guard: eight filter deps re-fire load; an old query
  // resolving after a newer one must not overwrite the fresh rows (2026-08-12)
  const seq = useRef(makeReqSeq());
  const load = () => {
    const q = `/runs?limit=${PAGE}&offset=${offset}` +
      (query ? `&mission_key=${encodeURIComponent(query)}` : "") +
      (pmoRef ? `&pmo_ref=${encodeURIComponent(pmoRef)}` : "") +
      (fromDate ? `&created_from=${fromDate}` : "") +
      (toDate ? `&created_to=${toDate}` : "") +
      `&sort=${sortKey}&dir=${sortDir}` +
      (grouped ? "&group_by=mission" : "");
    const mine = seq.current.next();
    return get(q).then((d) => {
      if (seq.current.isLatest(mine)) setData(d);   // drop out-of-order
    }).catch(() => {});
  };

  usePoll(load, 10000, [offset, query, pmoRef, fromDate, toDate,
                        sortKey, sortDir, grouped]);

  // per-run token magnitude for the cell tooltips: harness-reported total
  // when present, else the sum of the known splits (plain arithmetic —
  // deliberately NOT a 12th column: total ≈ cache reads on agent fleets
  // and would hide the mix the split columns exist to show)
  const runTotal = (r) => {
    if (r.total_tokens != null) return r.total_tokens;
    const known = [r.input_tokens, r.output_tokens, r.cache_read_tokens,
                   r.cache_write_tokens].filter((v) => v != null);
    return known.length ? known.reduce((a, b) => a + b, 0) : null;
  };
  const tokenCellTitle = (r) =>
    runTotal(r) != null ? `run total: ${tokens(runTotal(r))} tokens` : undefined;

  // effective displayed cost per row (ADR-0021): native first, estimates
  // fill gaps — flipped when the operator's Cost Inputs override is on
  const overrideOn = !!data.rate_card?.override_native;
  const costCell = (r) => {
    const showEst = overrideOn
      ? r.cost_usd_estimated != null
      : r.cost_usd == null && r.cost_usd_estimated != null;
    const eff = showEst ? r.cost_usd_estimated : r.cost_usd;
    if (eff == null) return <span>—</span>;
    if (!showEst) return <span>{usd(eff)}</span>;
    const title = `estimated (${data.rate_card?.rate_card_id || "rate card"})`
      + (overrideOn && r.cost_usd != null ? ` — harness reported ${usd(r.cost_usd)}` : "");
    return (
      <span title={title} className="text-neutral-500 dark:text-neutral-400">
        ~{usd(eff)}
      </span>
    );
  };

  const sortableTh = (key, label, extra = "") => {
    const active = sortKey === key;
    return (
      // whitespace-nowrap on th AND button: grouped mode's wide colSpan-4
      // mission cells squeeze the numeric columns, and "cache r" / an
      // active "started ▼" otherwise wrap into stacked header lines
      <th className={`whitespace-nowrap pr-3 ${extra}`}
        aria-sort={active ? (sortDir === "asc" ? "ascending" : "descending")
                          : undefined}>
        <button type="button" title={`Sort by ${label}`}
          className="inline-flex items-center gap-0.5 whitespace-nowrap rounded text-xs uppercase tracking-wide text-inherit hover:text-neutral-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:hover:text-neutral-100"
          onClick={() => {
            // first click on a column sorts DESC (you click "cost" wanting
            // the most expensive first); clicking again flips
            if (active) setSortDir(sortDir === "desc" ? "asc" : "desc");
            else { setSortKey(key); setSortDir("desc"); }
            setOffset(0);
          }}>
          {label}{active ? (sortDir === "desc" ? " ▼" : " ▲") : ""}
        </button>
      </th>
    );
  };

  const subtotalTitle = (t) =>
    `native ${usd(t.cost_usd)} · estimated ${usd(t.cost_usd_estimated)}`;

  const hasRows = grouped ? (data.groups || []).length > 0
                          : (data.runs || []).length > 0;

  // one renderer for both modes: flat rows and the runs inside a mission
  // group (grouped mode keeps pipeline order — seq — whatever the sort)
  const runRow = (r) => (
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
      <td className="pr-3 text-right text-xs tabular-nums" title={tokenCellTitle(r)}>{tokens(r.input_tokens)}</td>
      <td className="pr-3 text-right text-xs tabular-nums" title={tokenCellTitle(r)}>{tokens(r.output_tokens)}</td>
      <td className="pr-3 text-right text-xs tabular-nums" title={tokenCellTitle(r)}>{tokens(r.cache_read_tokens)}</td>
      <td className="pr-3 text-right text-xs tabular-nums" title={tokenCellTitle(r)}>{tokens(r.cache_write_tokens)}</td>
      <td className="pr-3 text-right text-xs tabular-nums">{costCell(r)}</td>
      <td>
        <a className="inline-flex items-center gap-0.5 text-xs text-accent-700 underline underline-offset-2 dark:text-accent-300"
          href={traceUrl(r.run_id)}
          onClick={(e) => e.stopPropagation()}
          target="_blank" rel="noopener">
          trace <ExternalLink size={10} aria-hidden />
        </a>
      </td>
      <td className="pl-2 text-right">
        {/* dispatched/running only — finalizing hides Stop too: the Dev has
            already exited and the backend 409s a stop there by design */}
        {["dispatched", "running"].includes(r.state) && (
          <Button size="sm" kind="danger-ghost"
            aria-label={`Stop run ${r.run_id}`}
            onClick={(e) => { e.stopPropagation(); setStopOneErr(""); setStopTarget(r); }}>
            Stop
          </Button>
        )}
      </td>
    </tr>
  );

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

  const doStopOne = async () => {
    if (!stopTarget) return;
    setStopOneBusy(true);
    setStopOneErr("");
    try {
      await send("POST", "/runs/" + encodeURIComponent(stopTarget.run_id) + "/stop");
      setStopTarget(null);
      load();
    } catch (e) {
      setStopOneErr(String(e.message || e));
    } finally {
      setStopOneBusy(false);
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
              { label: "Cost inputs…",
                desc: "Per-model rates behind estimated costs. Changes apply immediately.",
                onClick: () => setCostOpen(true) },
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
        {/* Every control sits in a fixed-width shrink-0 wrapper: the shared
            Input/Select carry w-full (a width class on them loses the
            Tailwind conflict), and without shrink-0 a crowded flex row
            crushes the filter to a sliver. flex-wrap + ml-auto keep the
            pagination cluster right-aligned even when it wraps to its own
            line on narrow viewports. */}
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <span className="relative w-64 shrink-0">
            <Input className="pr-7"
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
          <span className="w-40 shrink-0">
            <Select aria-label="Filter by PMO connector"
              value={pmoRef}
              onChange={(e) => { setPmoRef(e.target.value); setOffset(0); }}>
              <option value="">all PMOs</option>
              {(data.pmo_refs || []).map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </Select>
          </span>
          <span className="w-40 shrink-0">
            <Input type="date" aria-label="From date (UTC)"
              title="From (UTC calendar day)" value={fromDate}
              onChange={(e) => { setFromDate(e.target.value); setOffset(0); }} />
          </span>
          <span className="w-40 shrink-0">
            <Input type="date" aria-label="To date (UTC, inclusive)"
              title="To (UTC calendar day, inclusive)" value={toDate}
              onChange={(e) => { setToDate(e.target.value); setOffset(0); }} />
          </span>
          <label className="flex shrink-0 cursor-pointer items-center gap-1.5 whitespace-nowrap text-xs text-neutral-600 dark:text-neutral-300">
            <input type="checkbox" checked={grouped}
              onChange={(e) => { setGrouped(e.target.checked); setOffset(0); }} />
            Aggregate by mission
          </label>
          <span className="whitespace-nowrap text-xs text-neutral-500 dark:text-neutral-400">
            {grouped
              ? `${data.total} missions · ${data.total_runs ?? 0} runs`
              : `${data.total} runs`}
          </span>
          <span className="ml-auto flex shrink-0 items-center gap-3">
            <Button kind="ghost" size="sm" icon={ChevronLeft} disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE))}>newer</Button>
            <span className="whitespace-nowrap text-xs tabular-nums text-neutral-500 dark:text-neutral-400">
              {data.total === 0 ? "0" : `${offset + 1}–${Math.min(offset + PAGE, data.total)}`} of {data.total}{grouped ? " missions" : ""}
            </span>
            <Button kind="ghost" size="sm" disabled={offset + PAGE >= data.total}
              onClick={() => setOffset(offset + PAGE)}>
              older <ChevronRight size={13} aria-hidden />
            </Button>
          </span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[64rem] text-left text-sm">
            <thead className="text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
              <tr>
                <th className="py-1.5 pr-3">run</th>
                <th className="pr-3">mission</th>
                <th className="pr-3">state</th>
                {sortableTh("started", "started")}
                {sortableTh("duration", "duration")}
                {sortableTh("input_tokens", "in", "text-right")}
                {sortableTh("output_tokens", "out", "text-right")}
                {sortableTh("cache_read_tokens", "cache r", "text-right")}
                {sortableTh("cache_write_tokens", "cache w", "text-right")}
                {sortableTh("cost", "cost", "text-right")}
                <th>trace</th>
                <th aria-label="run actions" />
              </tr>
            </thead>
            <tbody>
              {!hasRows && (
                <tr><td colSpan={12} className="py-6 text-center text-sm text-neutral-500 dark:text-neutral-400">
                  No runs{filter ? " match this filter" : " yet"}.
                </td></tr>
              )}
              {data.totals && hasRows && (
                <tr data-testid="runs-totals"
                  className="border-t border-neutral-200 bg-stone-50/70 text-xs font-medium dark:border-neutral-700 dark:bg-neutral-900/70">
                  <td className="py-2 pr-3" colSpan={4}>
                    filtered totals — {data.total_runs ?? data.total} run{(data.total_runs ?? data.total) === 1 ? "" : "s"}
                    {data.totals.total_tokens_effective != null && (
                      <span className="font-normal text-neutral-500 dark:text-neutral-400"
                        title="Reported totals where the harness gives one, summed splits where it doesn't">
                        {" "}· {tokens(data.totals.total_tokens_effective)} tokens
                      </span>
                    )}
                  </td>
                  <td className="whitespace-nowrap pr-3 tabular-nums"
                    title="Sum of completed runs' durations (live runs excluded)">
                    {durationSeconds(data.totals.runtime_seconds)}
                  </td>
                  <td className="pr-3 text-right tabular-nums">{tokens(data.totals.input_tokens)}</td>
                  <td className="pr-3 text-right tabular-nums">{tokens(data.totals.output_tokens)}</td>
                  <td className="pr-3 text-right tabular-nums">{tokens(data.totals.cache_read_tokens)}</td>
                  <td className="pr-3 text-right tabular-nums">{tokens(data.totals.cache_write_tokens)}</td>
                  <td className="pr-3 text-right tabular-nums"
                    title={`native ${usd(data.totals.cost_usd)} · estimated ${usd(data.totals.cost_usd_estimated)}`}>
                    {usd(data.totals.cost_usd_effective)}
                  </td>
                  <td />
                  <td />
                </tr>
              )}
              {(grouped ? (data.groups || []) : []).map((g) => (
                <React.Fragment key={`${g.pmo_ref}:${g.mission_key}`}>
                  <tr data-testid="mission-group"
                    className="border-t border-neutral-200 bg-stone-50/40 text-xs dark:border-neutral-700 dark:bg-neutral-900/40">
                    <td className="py-1.5 pr-3" colSpan={4}>
                      <span className="font-semibold">{g.mission_key}</span>
                      <span className="text-neutral-500 dark:text-neutral-400">
                        {" "}· {g.pmo_ref} · {g.run_count} run{g.run_count === 1 ? "" : "s"}
                        {g.subtotal.total_tokens_effective != null &&
                          <> · {tokens(g.subtotal.total_tokens_effective)} tokens</>}
                      </span>
                    </td>
                    <td className="whitespace-nowrap pr-3 tabular-nums"
                      title="Sum of this mission's completed runs' durations">
                      {durationSeconds(g.subtotal.runtime_seconds)}
                    </td>
                    <td className="pr-3 text-right tabular-nums">{tokens(g.subtotal.input_tokens)}</td>
                    <td className="pr-3 text-right tabular-nums">{tokens(g.subtotal.output_tokens)}</td>
                    <td className="pr-3 text-right tabular-nums">{tokens(g.subtotal.cache_read_tokens)}</td>
                    <td className="pr-3 text-right tabular-nums">{tokens(g.subtotal.cache_write_tokens)}</td>
                    <td className="pr-3 text-right tabular-nums" title={subtotalTitle(g.subtotal)}>
                      {usd(g.subtotal.cost_usd_effective)}
                    </td>
                    <td />
                  <td />
                  </tr>
                  {g.runs.map(runRow)}
                </React.Fragment>
              ))}
              {(grouped ? [] : data.runs || []).map(runRow)}
            </tbody>
          </table>
        </div>
      </Card>
      {openRun && <RunTerminal run={openRun} onClose={() => setOpenRun(null)} />}
      {costOpen && (
        <CostInputsModal onClose={() => setCostOpen(false)} onSaved={load} />
      )}
      <ConfirmDialog
        open={!!stopTarget}
        title="Stop this run?"
        body={
          "The Dev container is stopped and this run is recorded as failed.\n" +
          "It counts toward the retry limit. The mission keeps its labels and " +
          "is picked up again on a later cycle — park it first if you don't " +
          "want that."
        }
        confirmLabel="Stop run"
        busy={stopOneBusy}
        error={stopOneErr}
        onConfirm={doStopOne}
        onCancel={() => !stopOneBusy && (setStopTarget(null), setStopOneErr(""))}
      />
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
