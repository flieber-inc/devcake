import React, { useEffect, useState } from "react";
import {
  Activity, Play, Pause, Bot, ExternalLink, Workflow, ScrollText,
  BookOpen, Hand, GitMerge, SquareTerminal,
} from "lucide-react";
import { Card } from "../components/Card.jsx";
import Alert from "../components/Alert.jsx";
import StatusDot from "../components/StatusDot.jsx";
import StatusPill from "../components/StatusPill.jsx";
import RunTerminal, { TERMINAL_STATES } from "../components/RunTerminal.jsx";
import StageGlyph from "../components/StageGlyph.jsx";
import usePoll from "../lib/usePoll.js";
import { SERVICES, serviceValue, devTypeState } from "../lib/services.js";
import { relTime, fullTime } from "../lib/format.js";
import { get } from "../api.js";

const STAGES = ["ONBOARD", "PLAN", "EXECUTE", "REVIEW"];

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
          {title} <ExternalLink size={11} className="text-neutral-500 dark:text-neutral-400" aria-hidden />
        </span>
        <span className="mt-0.5 block text-xs text-neutral-500 dark:text-neutral-400">{desc}</span>
      </span>
    </a>
  );
}

// The hero is an answer, not a grid: the operator opens this page asking
// "do I need to do anything?" — so the page opens with exactly that,
// set in the display face (accent period as the cherry on top).
function Masthead({ health, humanCount, criticalCount = 0, runsTotal }) {
  const known = health.app !== undefined;
  const svc = SERVICES.map(([k]) => serviceValue(health, k));
  const eyebrow = !known
    ? "waiting for the backend"
    : svc.some((v) => v === false)
      ? "a service is down — see the sidebar"
      : svc.every((v) => v !== undefined)
        ? "all services healthy"
        : "service health unknown";
  const active = health.active_runs || 0;
  return (
    <div>
      <p className="font-mono text-[11px] uppercase tracking-widest text-neutral-500 dark:text-neutral-400">
        {eyebrow}
      </p>
      <h1 className="mt-1 font-display text-3xl font-extrabold tracking-tight">
        {!known ? "DevCake"
          : humanCount > 0
            ? `${humanCount} thing${humanCount > 1 ? "s" : ""} need${humanCount > 1 ? "" : "s"} you`
            : criticalCount > 0
              ? `${criticalCount} critical warning${criticalCount > 1 ? "s" : ""}`
              : "Nothing needs you"}
        <span className="text-accent-600 dark:text-accent-400">.</span>
      </h1>
      <p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">
        {!known ? "no data received yet" : (
          <>
            <span className="font-semibold text-neutral-700 dark:text-neutral-200">
              {active} Dev{active === 1 ? "" : "s"} baking
            </span>
            {" · "}intake {health.intake_paused ? "PAUSED" : "ON"}
            {runsTotal != null && <>{" · "}{runsTotal} run{runsTotal === 1 ? "" : "s"} recorded</>}
          </>
        )}
      </p>
    </div>
  );
}

// "In the oven": the pipeline strip — active runs by stage, the signature
// layer-cake device in dashboard form. Each cell links to the Runs page.
function OvenStrip({ runs }) {
  const active = (runs || []).filter((r) => !TERMINAL_STATES.includes(r.state));
  const total = active.length;
  return (
    <Card className="p-4">
      <div className="mb-3 flex items-baseline gap-2">
        <h3 className="text-sm font-semibold tracking-tight">In the oven</h3>
        <span className="text-xs text-neutral-500 dark:text-neutral-400">
          {total === 0 ? "no active runs" : `${total} active run${total > 1 ? "s" : ""} by stage`}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {STAGES.map((s) => {
          const n = active.filter((r) => r.mission_type === s).length;
          return (
            <a key={s} href="#/runs"
              className={`rounded-lg border p-3 transition ${
                n > 0
                  ? "border-accent-200 bg-accent-50/60 hover:border-accent-300 dark:border-accent-900 dark:bg-accent-950/30 dark:hover:border-accent-800"
                  : "border-neutral-200 hover:border-neutral-300 dark:border-neutral-800 dark:hover:border-neutral-700"
              }`}>
              <div className="font-mono text-[10px] uppercase tracking-widest text-neutral-500 dark:text-neutral-400">
                {s}
              </div>
              <div className={`font-display text-xl font-extrabold tabular-nums tracking-tight ${
                n > 0 ? "text-accent-700 dark:text-accent-300" : ""
              }`}>
                {n}
              </div>
              <div className="text-[11px] text-neutral-500 dark:text-neutral-400">
                {n > 0 ? "baking" : "idle"}
              </div>
            </a>
          );
        })}
      </div>
    </Card>
  );
}

// Fixed dismiss key for the setup checklist's internal-forge path (persisted
// via config.dismissed_alerts — same instant PUT as advisory alert dismiss).
export const SETUP_INTERNAL_FORGE_KEY = "setup-checklist:internal-forge";

// First-run checklist ("Let's get baking"): three steps to the first adopted
// mission, checked from data the app already has. Retires itself the moment
// everything passes — it never nags a configured system.
//
// The repo step is satisfied by an external work-repo token, a healthy
// internal forge (zero-repo path), or an explicit operator dismiss.
function SetupChecklist({ health, dismissedKeys = [], onDismissInternalForge }) {
  const [checks, setChecks] = useState(null); // {pmoOk, repoOk, devOk}
  useEffect(() => {
    (async () => {
      try {
        const cfg = await get("/config");
        const pmos = (cfg.pmos || []).filter((p) => p.team_key);
        const repos = cfg.repos || [];
        const conn = [
          ...pmos.map((p) => `pmo:${p.name}:api_key`),
          ...repos.map((r) => `repo:${r.name}:token`),
        ].join(",");
        const sc = conn ? await get(`/secrets-check?conn=${encodeURIComponent(conn)}`) : { conn: {} };
        const pmoOk = pmos.some((p) => sc.conn[`pmo:${p.name}:api_key`]?.present);
        const repoOk = repos.some((r) => sc.conn[`repo:${r.name}:token`]?.present);
        const [dts, harnesses] = await Promise.all([get("/dev-types"), get("/harnesses")]);
        const envNames = [...new Set(dts.flatMap(
          (d) => harnesses[d.harness_template]?.credential_env || []))];
        const hs = envNames.length
          ? await get(`/secrets-check?harness=${encodeURIComponent(envNames.join(","))}`)
          : { harness: {} };
        const devOk = dts.some((d) =>
          (d.secrets_present || []).length > 0 ||
          (harnesses[d.harness_template]?.credential_env || [])
            .some((v) => hs.harness[v]?.present));
        setChecks({ pmoOk, repoOk, devOk });
      } catch { setChecks(null); }
    })();
  }, []);
  if (!checks) return null;
  const internalForgeOk = Boolean(health?.internal_forge?.ok);
  const dismissedInternal = dismissedKeys.includes(SETUP_INTERNAL_FORGE_KEY);
  const repoStepOk = checks.repoOk || internalForgeOk || dismissedInternal;
  if (checks.pmoOk && repoStepOk && checks.devOk) return null;
  const repoNote = checks.repoOk
    ? null
    : internalForgeOk
      ? "(internal forge)"
      : dismissedInternal
        ? "(using internal forge)"
        : null;
  const steps = [
    { ok: checks.pmoOk, text: "Connect a PMO and store its API key", href: "#/config/pmo", go: "Configuration" },
    {
      ok: repoStepOk,
      text: "Add a repository and store its Access token — or use the internal forge",
      href: "#/repos",
      go: "Repositories",
      note: repoNote,
      // secondary dismiss when the step is still open
      dismiss: !repoStepOk && onDismissInternalForge
        ? { label: "I'll work with the internal forge", onClick: onDismissInternalForge }
        : null,
    },
    { ok: checks.devOk, text: "Give a Dev Type credentials", href: "#/config/dev-types", go: "Dev Types" },
  ];
  const done = steps.filter((s) => s.ok).length;
  return (
    <Card className="p-5">
      <div className="flex items-start gap-4">
        {/* the half-baked stack: one layer fills per completed step (+base) */}
        <span className="mt-1 flex w-9 flex-col-reverse gap-1" role="img"
          aria-label={`setup ${done} of 3 steps complete`}>
          {[0, 1, 2, 3].map((i) => (
            <span key={i}
              style={{ width: `${100 - i * 8}%` }}
              className={`h-1.5 rounded-full ${
                i <= done ? "bg-accent-500 dark:bg-accent-400" : "bg-neutral-200 dark:bg-neutral-800"
              }`} />
          ))}
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="font-display text-lg font-extrabold tracking-tight">
            Let&apos;s get baking<span className="text-accent-600 dark:text-accent-400">.</span>
          </h3>
          <p className="mt-0.5 text-sm text-neutral-500 dark:text-neutral-400">
            {3 - done} step{done === 2 ? "" : "s"} to DevCake&apos;s first adopted mission.
          </p>
          <ul className="mt-3 divide-y divide-neutral-100 dark:divide-neutral-800">
            {steps.map((s) => (
              <li key={s.text} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm">
                <span className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs ${
                  s.ok
                    ? "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-400"
                    : "border-[1.5px] border-dashed border-neutral-300 dark:border-neutral-700"
                }`}>
                  {s.ok ? "✓" : ""}
                </span>
                <span className={`min-w-0 flex-1 ${s.ok ? "text-neutral-500 line-through decoration-neutral-300 dark:text-neutral-400 dark:decoration-neutral-700" : ""}`}>
                  {s.text}
                  {s.note && (
                    <span className="ml-1.5 text-xs font-normal no-underline text-neutral-400 dark:text-neutral-500">
                      {s.note}
                    </span>
                  )}
                </span>
                {!s.ok && (
                  <span className="ml-auto flex shrink-0 flex-wrap items-center justify-end gap-x-3 gap-y-1">
                    {s.dismiss && (
                      <button type="button" onClick={s.dismiss.onClick}
                        title="Missions without a work-repo set use the bundled Gitea; deliverables attach to the PMO."
                        className="text-xs font-medium text-neutral-500 hover:text-neutral-700 dark:text-neutral-400 dark:hover:text-neutral-200">
                        {s.dismiss.label}
                      </button>
                    )}
                    <a href={s.href}
                      className="text-xs font-semibold text-accent-700 hover:underline dark:text-accent-300">
                      {s.go} →
                    </a>
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </Card>
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
  dismissedKeys = [], onDismissInternalForge,
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
      <Masthead health={health} humanCount={humanCount}
        criticalCount={alerts.filter((a) => a.severity === "critical").length}
        runsTotal={recent?.total} />

      <SetupChecklist health={health}
        dismissedKeys={dismissedKeys}
        onDismissInternalForge={onDismissInternalForge} />

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
                className="text-xs text-neutral-500 dark:text-neutral-400 underline underline-offset-2 hover:text-neutral-600 dark:hover:text-neutral-300"
              >
                {dismissedAlerts.length} dismissed warning{dismissedAlerts.length > 1 ? "s" : ""} —{" "}
                {showDismissed ? "hide" : "show"}
              </button>
              {showDismissed && (
                <ul className="mt-1.5 space-y-1">
                  {dismissedAlerts.map((a) => (
                    <li key={a.id}
                      className="flex flex-wrap items-center gap-2 rounded-lg border border-neutral-200 px-3 py-1.5 text-xs text-neutral-500 dark:text-neutral-400 dark:border-neutral-800">
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
          <span className="font-display text-2xl font-extrabold tabular-nums tracking-tight">
            {health.active_runs ?? "—"}
          </span>
        </Stat>
        <Stat icon={paused ? Pause : Play} label="Mission intake">
          {health.app === undefined ? (
            <span className="font-display text-2xl font-extrabold tracking-tight">—</span>
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
            <span className="font-display text-2xl font-extrabold tracking-tight">—</span>
          ) : (
            <div className="space-y-1">
              <span className="font-display text-2xl font-extrabold tabular-nums tracking-tight">
                {devsOk}/{devStates.length}
              </span>
              <div className="flex flex-wrap gap-x-3 gap-y-0.5">
                {devStates.map((s) => (
                  <StatusDot key={s.dt.name} state={s.state} label={s.dt.name}
                    title={s.why || (s.state === "running" ? "run in progress" : "available")} />
                ))}
              </div>
              {/* visible legend — the color code shouldn't live only in tooltips */}
              <p className="text-[10px] text-neutral-500 dark:text-neutral-400">
                green available · blue running · red broken
              </p>
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
          <span className={`font-display text-2xl font-extrabold tabular-nums tracking-tight ${
            humanCount > 0 ? "text-amber-600 dark:text-amber-400" : ""
          }`}>
            {humanCount}
          </span>
        </Stat>
      </div>

      <OvenStrip runs={recent?.runs} />

      <div className="grid gap-3 lg:grid-cols-3">
        <Card className="p-4 lg:col-span-2">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-semibold tracking-tight">Recent runs</h3>
            <a href="#/runs" className="text-xs font-medium text-accent-700 hover:underline dark:text-accent-300">
              View all →
            </a>
          </div>
          {!recent ? (
            <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading…</p>
          ) : recent.runs.length === 0 ? (
            <p className="text-sm text-neutral-500 dark:text-neutral-400">No runs yet.</p>
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
                      <td className="py-2 pr-3">
                        <span className="flex items-center gap-2">
                          {r.mission_type && <StageGlyph stage={r.mission_type} size={12} />}
                          <button type="button"
                            onClick={(e) => { e.stopPropagation(); setOpenRun(r); }}
                            title="Open the run terminal"
                            className="inline-flex items-center gap-1.5 rounded font-mono text-xs underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60">
                            <SquareTerminal size={11} className="shrink-0 text-neutral-500 dark:text-neutral-400" aria-hidden />
                            {r.run_id}
                          </button>
                        </span>
                      </td>
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
          {health.internal_forge && (
            <QuickLink href={health.internal_forge.ui_url || "http://localhost:3300"}
              icon={GitMerge} title="Gitea (internal forge)"
              desc="The bundled forge — internal repos, PRs and history" />
          )}
          <QuickLink href="https://github.com/fidecastro/devcake" icon={BookOpen}
            title="Spec & source" desc="DevCake design docs and repository" />
        </div>
      </div>

      {openRun && <RunTerminal run={openRun} onClose={() => setOpenRun(null)} />}
    </div>
  );
}
