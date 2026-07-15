import React, { useState } from "react";
import {
  Activity, Play, Pause, Bot, ExternalLink, Workflow, ScrollText,
  BookOpen, Hand, GitMerge,
} from "lucide-react";
import PageHeader from "../components/PageHeader.jsx";
import { Card } from "../components/Card.jsx";
import Alert from "../components/Alert.jsx";
import StatusDot from "../components/StatusDot.jsx";
import StatusPill from "../components/StatusPill.jsx";
import RunTerminal from "../components/RunTerminal.jsx";
import usePoll from "../lib/usePoll.js";
import { devTypeState } from "../lib/services.js";
import { relTime, fullTime } from "../lib/format.js";
import { get } from "../api.js";

const ext = window.DEVCAKE || {};

function Stat({ icon: Icon, label, children, href, onClick }) {
  const body = (
    <>
      <div className="flex items-center gap-2 text-xs font-medium text-neutral-500 dark:text-neutral-400">
        <Icon size={14} aria-hidden />
        {label}
      </div>
      <div className="mt-2">{children}</div>
    </>
  );
  if (href || onClick) {
    return (
      <Card className="transition hover:border-accent-300 dark:hover:border-accent-800">
        {href ? (
          <a href={href} className="block p-4">{body}</a>
        ) : (
          <button onClick={onClick} className="block w-full p-4 text-left">{body}</button>
        )}
      </Card>
    );
  }
  return <Card className="p-4">{body}</Card>;
}

function QuickLink({ href, icon: Icon, title, desc }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener"
      className="flex items-start gap-3 rounded-card border border-neutral-200 bg-surface-raised p-4 shadow-card transition hover:border-accent-300 dark:border-neutral-800 dark:bg-surface-raised-dark dark:hover:border-accent-800"
    >
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent-50 text-accent-700 dark:bg-accent-950/70 dark:text-accent-300">
        <Icon size={16} aria-hidden />
      </span>
      <span className="min-w-0">
        <span className="flex items-center gap-1 text-sm font-semibold">
          {title} <ExternalLink size={11} className="text-neutral-400" aria-hidden />
        </span>
        <span className="mt-0.5 block text-xs text-neutral-500 dark:text-neutral-400">{desc}</span>
      </span>
    </a>
  );
}

// One unified "your action needed" list: DEVCAKE-MERGE hand-offs and
// DEVCAKE-NEEDS-HUMAN batons. PMO-agnostic by construction — it renders the
// "KEY: reason — url" strings the backend hands it, nothing more.
function splitEntry(entry) {
  const i = entry.lastIndexOf(" — ");
  const [text, url] = i > -1 ? [entry.slice(0, i), entry.slice(i + 3)] : [entry, null];
  const j = text.indexOf(": ");
  const [key, reason] = j > -1 ? [text.slice(0, j), text.slice(j + 2)] : [null, text];
  return { key, reason, url };
}

function NeedsHumanPanel({ merge, attention }) {
  const rows = [
    ...merge.map((e) => ({ ...splitEntry(e), kind: "merge", raw: e })),
    ...attention.map((e) => ({ ...splitEntry(e), kind: "attention", raw: e })),
  ];
  if (rows.length === 0) return null;
  return (
    <Card id="needs-human" className="scroll-mt-6 border-amber-200 p-4 dark:border-amber-900">
      <div className="mb-3 flex items-center gap-2">
        <Hand size={15} className="text-amber-600 dark:text-amber-400" aria-hidden />
        <h3 className="text-sm font-semibold tracking-tight">Needs Human Action</h3>
        <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-800 dark:bg-amber-950 dark:text-amber-300">
          {rows.length}
        </span>
      </div>
      <ul className="divide-y divide-neutral-100 dark:divide-neutral-800">
        {rows.map((r) => (
          <li key={r.raw} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm">
            {r.key && <span className="font-mono text-xs font-semibold">{r.key}</span>}
            <span
              className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
                r.kind === "merge"
                  ? "bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300"
                  : "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300"
              }`}
            >
              {r.kind === "merge" ? <GitMerge size={10} aria-hidden /> : <Hand size={10} aria-hidden />}
              {r.kind === "merge" ? "merge" : "attention"}
            </span>
            <span className="min-w-0 flex-1 text-neutral-600 dark:text-neutral-300">{r.reason}</span>
            {r.url && (
              <a
                href={r.url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-xs font-medium text-accent-700 underline underline-offset-2 dark:text-accent-300"
              >
                open <ExternalLink size={10} aria-hidden />
              </a>
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}

export default function OverviewPage({
  health, alerts, dismissedAlerts = [], onDismissAlert, onRestoreAlert,
}) {
  const [recent, setRecent] = useState(null);
  const [devTypes, setDevTypes] = useState(null);
  const [openRun, setOpenRun] = useState(null);
  const [showDismissed, setShowDismissed] = useState(false);
  // limit=25 (not 5): the Devs card scans for ACTIVE runs per dev type; the
  // Recent-runs card slices the first five below
  usePoll(() => get("/runs?limit=25&offset=0").then(setRecent).catch(() => {}), 10000);
  usePoll(() => get("/dev-types").then(setDevTypes).catch(() => {}), 10000);

  const devStates = (devTypes || []).map((dt) => ({
    dt, ...devTypeState(dt, health, recent?.runs),
  }));
  const devsOk = devStates.filter((s) => s.state !== "broken").length;
  const paused = !!health.intake_paused;
  const merge = Object.values(health.merge_handoffs || {});
  const attention = Object.values(health.needs_human || {});
  const humanCount = merge.length + attention.length;

  return (
    <div className="space-y-6">
      <PageHeader title="Overview" subtitle="System status at a glance" />

      {(alerts.length > 0 || dismissedAlerts.length > 0) && (
        <div className="space-y-2">
          {alerts.map((a) => (
            <Alert key={a.id} {...a}
              onDismiss={a.dismissable && onDismissAlert ? () => onDismissAlert(a) : undefined} />
          ))}
          {dismissedAlerts.length > 0 && (
            <div>
              <button
                onClick={() => setShowDismissed(!showDismissed)}
                className="text-xs text-neutral-400 underline underline-offset-2 hover:text-neutral-600 dark:hover:text-neutral-300"
              >
                {dismissedAlerts.length} dismissed warning{dismissedAlerts.length > 1 ? "s" : ""} —{" "}
                {showDismissed ? "hide" : "show"}
              </button>
              {showDismissed && (
                <ul className="mt-1.5 space-y-1">
                  {dismissedAlerts.map((a) => (
                    <li key={a.id}
                      className="flex flex-wrap items-center gap-2 rounded-lg border border-neutral-200 px-3 py-1.5 text-xs text-neutral-400 dark:border-neutral-800">
                      <span className="font-medium">{a.title}</span>
                      <span className="min-w-0 flex-1 truncate">{a.body}</span>
                      <button
                        onClick={() => onRestoreAlert && onRestoreAlert(a)}
                        className="rounded border border-neutral-300 px-1.5 py-0.5 font-medium hover:bg-stone-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
                      >
                        Restore
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}

      <NeedsHumanPanel merge={merge} attention={attention} />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat icon={Activity} label="Active runs" href="#/runs">
          <span className="text-2xl font-bold tabular-nums tracking-tight">
            {health.active_runs ?? "—"}
          </span>
        </Stat>
        <Stat icon={paused ? Pause : Play} label="Mission intake">
          {health.app === undefined ? (
            <span className="text-2xl font-bold tracking-tight">—</span>
          ) : (
            <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ${
              paused
                ? "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300"
                : "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300"
            }`}>
              {paused ? "PAUSED" : "ON"}
            </span>
          )}
        </Stat>
        <Stat icon={Bot} label="Devs">
          {/* the Dev fleet in the Runs-table color code (founder decision):
              green available · blue running · red broken (breaker latched
              or no credentials). Service health lives in the sidebar. */}
          {devTypes === null ? (
            <span className="text-2xl font-bold tracking-tight">—</span>
          ) : (
            <div className="space-y-1">
              <span className="text-2xl font-bold tabular-nums tracking-tight">
                {devsOk}/{devStates.length}
              </span>
              <div className="flex flex-wrap gap-x-3 gap-y-0.5">
                {devStates.map((s) => (
                  <StatusDot key={s.dt.name} state={s.state} label={s.dt.name}
                    title={s.why || (s.state === "running" ? "run in progress" : "available")} />
                ))}
              </div>
            </div>
          )}
        </Stat>
        <Stat
          icon={Hand}
          label="Needs human"
          onClick={humanCount > 0
            ? () => document.getElementById("needs-human")?.scrollIntoView({ behavior: "smooth" })
            : undefined}
        >
          <span className={`text-2xl font-bold tabular-nums tracking-tight ${
            humanCount > 0 ? "text-amber-600 dark:text-amber-400" : ""
          }`}>
            {humanCount}
          </span>
        </Stat>
      </div>

      <div className="grid gap-3 lg:grid-cols-3">
        <Card className="p-4 lg:col-span-2">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-semibold tracking-tight">Recent runs</h3>
            <a href="#/runs" className="text-xs font-medium text-accent-700 hover:underline dark:text-accent-300">
              View all →
            </a>
          </div>
          {!recent ? (
            <p className="text-sm text-neutral-400">Loading…</p>
          ) : recent.runs.length === 0 ? (
            <p className="text-sm text-neutral-400">No runs yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <tbody>
                  {recent.runs.slice(0, 5).map((r) => (
                    <tr
                      key={r.run_id}
                      onClick={() => setOpenRun(r)}
                      title="Click to open the run terminal"
                      className="cursor-pointer border-t border-neutral-100 first:border-t-0 hover:bg-stone-50 dark:border-neutral-800 dark:hover:bg-neutral-900"
                    >
                      <td className="py-2 pr-3 font-mono text-xs">{r.run_id}</td>
                      <td className="pr-3 font-mono text-xs text-neutral-500 dark:text-neutral-400">
                        {r.mission_key || "—"}
                      </td>
                      <td className="pr-3"><StatusPill state={r.state} verdict={r.verdict} /></td>
                      <td className="whitespace-nowrap text-xs text-neutral-500 dark:text-neutral-400"
                        title={fullTime(r.started_at)}>
                        {relTime(r.started_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
        <div className="space-y-3">
          <QuickLink href={ext.daguUrl || "http://localhost:8525"} icon={Workflow}
            title="Dagu" desc="Executor DAGs and step-level history" />
          <QuickLink href={ext.ooUrl || "http://localhost:5080"} icon={ScrollText}
            title="OpenObserve" desc="Traces, logs and token costs per run" />
          <QuickLink href="https://github.com/fidecastro/devcake" icon={BookOpen}
            title="Spec & source" desc="DevCake design docs and repository" />
        </div>
      </div>

      {openRun && <RunTerminal run={openRun} onClose={() => setOpenRun(null)} />}
    </div>
  );
}
