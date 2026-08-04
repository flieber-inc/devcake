import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Plus, RefreshCw } from "lucide-react";
import PageHeader from "../components/PageHeader.jsx";
import Button from "../components/Button.jsx";
import MissionRow from "../components/MissionRow.jsx";
import MissionDrawer from "../components/MissionDrawer.jsx";
import NewMissionDialog from "../components/NewMissionDialog.jsx";
import { ConfirmDialog } from "../components/Modal.jsx";
import { Select } from "../components/Field.jsx";
import { get, send } from "../api.js";
import usePoll from "../lib/usePoll.js";
import { bucketize, COLUMNS } from "../lib/board.js";

// Board refresh cadence — /missions reflects the last PMO poll (~30s at the
// server); polling at 10s here keeps the UI fresh without leading the operator
// to expect a faster PMO round-trip than exists.
const POLL_MS = 10_000;

// Done is history, not work: the section previews the newest few and unfolds
// on demand (bucketize itself already caps Done at its 30 newest).
const DONE_PREVIEW = 10;

// Sections render Needs human first (it answers "what needs me"), then the
// pipeline in stage order, Done last. Empty stages exist only in the strip.
const SECTION_ORDER = [
  "needs_human",
  ...COLUMNS.map((c) => c.id).filter((id) => id !== "needs_human"),
];

// 2026-08 reviewer round: per-operator stage visibility. Hiding a stage
// removes its SECTION from the list only — the strip pill stays, dimmed,
// still carrying the live count, so a stage with work in it can never
// become invisible-invisible; clicking the dimmed pill shows it again.
// Persisted per browser (localStorage, the devcake-sidebar precedent) —
// a view preference, never config.
const HIDDEN_STAGES_KEY = "devcake-missions-hidden";

function readHiddenStages() {
  try {
    const v = JSON.parse(localStorage.getItem(HIDDEN_STAGES_KEY) || "[]");
    return Array.isArray(v)
      ? v.filter((id) => COLUMNS.some((c) => c.id === id))
      : [];
  } catch {
    return [];
  }
}

// Per-operator PMO filter (view preference, never config — the
// devcake-missions-hidden precedent). The stored value is validated against
// the CURRENT teams map at render time, so a leftover filter for a deleted/
// renamed instance — or any filter on a single-PMO deployment — is forcibly
// inert and can never hide missions invisibly (docs/11 §2b rule).
const PMO_FILTER_KEY = "devcake-missions-pmo";

function readPmoFilter() {
  try {
    const v = localStorage.getItem(PMO_FILTER_KEY) || "";
    return typeof v === "string" ? v : "";
  } catch {
    return "";
  }
}

// The MAJORITY reason of a bucket (e.g. "terminal — ignored" on a Done
// section) — hoisted into the section header so rows only spell out
// deviations. Majority, not unanimity: one odd row must not force the
// other 29 to repeat the same line (it shows its own reason inline).
function sharedReason(bucket) {
  const counts = new Map();
  for (const r of bucket) {
    if (r.reason && !r.schedulable) counts.set(r.reason, (counts.get(r.reason) || 0) + 1);
  }
  let best = null;
  for (const [reason, n] of counts) {
    if (n > bucket.length / 2 && (!best || n > counts.get(best))) best = reason;
  }
  return best;
}

// Copy shared with ConfirmDialog per DESIGN.md §7 (honest, sentence case).
const CONFIRM_COPY = {
  park: {
    title: "Park this mission?",
    body:
      "DevCake stops scheduling new work on this mission until you unpark it.\n" +
      "Any run already in flight keeps going — stop it from the run list if needed.\n" +
      "Nothing is deleted; the mission stays in your PMO.",
    confirmLabel: "Park mission",
  },
};

// "Last polled Ns ago" — honest cadence disclosure per docs/11 §0. The PMO is
// the source of truth (INV-1); the SPA is only ever as fresh as the last poll.
function timeAgoSeconds(iso) {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return null;
  return Math.max(0, Math.floor((Date.now() - then) / 1000));
}

function formatAgo(sec) {
  if (sec == null) return "never";
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const m = Math.floor(sec / 60);
  return m < 60 ? `${m}m ago` : `${Math.floor(m / 60)}h ago`;
}

export default function MissionsPage() {
  const [data, setData] = useState({ missions: [], adoption_mode: "auto", teams: {} });
  const [pollState, setPollState] = useState({
    last_poll_at: null,
    poll_interval_seconds: 30,
    poll_degraded: {},
    dependency_cycles: [],
  });
  const [error, setError] = useState("");
  // per-mission optimistic overrides (pmo_id → { labels, syncing:true }).
  // Cleared once the next /missions poll confirms the change.
  const [pending, setPending] = useState({});
  const [openMission, setOpenMission] = useState(null);
  const [showAllDone, setShowAllDone] = useState(false);
  const [hiddenStages, setHiddenStages] = useState(readHiddenStages);
  const [pmoFilter, setPmoFilterState] = useState(readPmoFilter);
  const setPmoFilter = (name) => {
    setPmoFilterState(name);
    try {
      localStorage.setItem(PMO_FILTER_KEY, name);
    } catch { /* storage unavailable — the filter still works this session */ }
  };
  const toggleStage = (id) =>
    setHiddenStages((prev) => {
      const next = prev.includes(id)
        ? prev.filter((x) => x !== id)
        : [...prev, id];
      try {
        localStorage.setItem(HIDDEN_STAGES_KEY, JSON.stringify(next));
      } catch { /* storage unavailable — the toggle still works this session */ }
      return next;
    });
  const [confirmAction, setConfirmAction] = useState(null); // {pmo_id, action}
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [flash, setFlash] = useState("");
  const [pollBusy, setPollBusy] = useState(false);
  const [composerOpen, setComposerOpen] = useState(false);

  // post-create honesty: name what exists WHERE, disclose any failed
  // attachment by name (the mission exists either way — ADR-0030), then
  // fire the existing 409-tolerant poll so it appears in seconds not ~30s
  const onMissionCreated = (result) => {
    setComposerOpen(false);
    const fails = result.attachment_failures || [];
    const failNote = fails.length
      ? ` ${fails.length} attachment${fails.length === 1 ? "" : "s"} failed (${fails.map((f) => f.name).join(", ")}) — attach ${fails.length === 1 ? "it" : "them"} in the PMO.`
      : "";
    setFlash(`${result.key} created in ${result.instance} — appears on the board after the next poll.${failNote}`);
    setTimeout(() => setFlash(""), 8000);
    // silent poll (not doPoll — its own flash would clobber the creation
    // notice); 409 = a cycle is already running = picked up anyway
    send("POST", "/poll/run", {}).then(() => load()).catch(() => {});
  };
  // 1s ticker so "Last polled Ns ago" counts up between fetches
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((n) => (n + 1) % 3600), 1000);
    return () => clearInterval(t);
  }, []);

  const load = useCallback(async () => {
    try {
      const [missionsBody, healthBody] = await Promise.all([
        get("/missions"),
        get("/health").catch(() => null),
      ]);
      setData(missionsBody);
      if (healthBody) {
        setPollState({
          last_poll_at: healthBody.last_poll_at || null,
          poll_interval_seconds: healthBody.poll_interval_seconds || 30,
          poll_degraded: healthBody.poll_degraded || {},
          dependency_cycles: healthBody.dependency_cycles || [],
        });
      }
      setPending((prev) => {
        const next = {};
        for (const [pmo_id, entry] of Object.entries(prev)) {
          const row = missionsBody.missions.find((m) => m.pmo_id === pmo_id);
          const serverLabels = row ? [...(row.labels || [])].sort().join(",") : null;
          const projected = [...(entry.labels || [])].sort().join(",");
          if (serverLabels !== projected) next[pmo_id] = entry;
        }
        return next;
      });
      setError("");
    } catch (e) {
      setError(String(e.message || e));
    }
  }, []);

  usePoll(load, POLL_MS);

  const rows = useMemo(() => {
    return (data.missions || []).map((row) => {
      const p = pending[row.pmo_id];
      if (!p) return row;
      return { ...row, labels: p.labels };
    });
  }, [data.missions, pending]);

  // Provenance surfaces only when there is more than one PMO to tell apart
  // (ADR-0009's N>1 convention; a fleet-wide-identical badge has no scan
  // value). `teams` lists exactly the CONFIGURED instances and rides the
  // same fetch as the rows, so gate and options can't be a poll out of sync.
  const teamNames = Object.keys(data.teams || {});
  const multiPmo = teamNames.length >= 2;
  const activeFilter = multiPmo && teamNames.includes(pmoFilter) ? pmoFilter : "";

  // Filter UPSTREAM of bucketize so the strip counts follow: the pills are
  // jump-buttons to sections — a pill claiming 5 over a section rendering 2
  // reads as a bug. The visible "n of m" indicator keeps the fleet size
  // honest while the filter narrows.
  const filteredRows = useMemo(
    () => (activeFilter ? rows.filter((r) => r.instance === activeFilter) : rows),
    [rows, activeFilter]
  );

  const buckets = useMemo(
    () => bucketize(filteredRows, data.adoption_mode),
    [filteredRows, data.adoption_mode]
  );

  const doAction = async (pmo_id, action) => {
    try {
      const result = await send("POST", `/missions/${encodeURIComponent(pmo_id)}/actions`, {
        action,
      });
      setPending((prev) => ({
        ...prev,
        [pmo_id]: { labels: result.labels || [], syncing: true },
      }));
      setFlash(`${action} sent — waiting for the next poll to confirm.`);
      setTimeout(() => setFlash(""), 4000);
    } catch (e) {
      setError(`Action ${action} failed: ${e.message || e}`);
    }
  };

  const requestAction = (pmo_id, action) => {
    if (CONFIRM_COPY[action]) {
      setConfirmAction({ pmo_id, action });
    } else {
      doAction(pmo_id, action);
    }
  };

  const confirmProceed = async () => {
    if (!confirmAction) return;
    setConfirmBusy(true);
    try {
      await doAction(confirmAction.pmo_id, confirmAction.action);
      setConfirmAction(null);
    } finally {
      setConfirmBusy(false);
    }
  };

  const doPoll = async () => {
    if (pollBusy) return;
    setPollBusy(true);
    setError("");
    try {
      const result = await send("POST", "/poll/run", {});
      const ms = result?.duration_ms;
      setFlash(
        `Poll finished${typeof ms === "number" ? ` in ${(ms / 1000).toFixed(1)}s` : ""} — refreshing the board.`
      );
      setTimeout(() => setFlash(""), 4000);
      await load();
    } catch (e) {
      // 409 = another cycle in flight (periodic or manual); honest banner.
      // Key off e.status (api.js attaches it deliberately) so this doesn't
      // silently stop matching if the backend detail copy is reworded.
      if (e.status === 409) {
        setFlash("A poll cycle is already running — try again in a moment.");
        setTimeout(() => setFlash(""), 4000);
      } else {
        setError(`Poll now failed: ${e.message || e}`);
      }
    } finally {
      setPollBusy(false);
    }
  };

  // recompute derived cadence text on every tick — no state coupling
  void tick;
  const secondsSincePoll = timeAgoSeconds(pollState.last_poll_at);
  const nextInSeconds =
    secondsSincePoll == null
      ? null
      : Math.max(0, (pollState.poll_interval_seconds || 30) - secondsSincePoll);
  const degradedEntries = Object.entries(pollState.poll_degraded || {});
  // Degraded overrides the honest cadence: /health.last_poll_at ticks up even
  // when every instance segment is failing (main.py finally-block stamps it),
  // so "Last polled just now" without this override would be a lie. See
  // review finding #1.
  const cadenceLine =
    degradedEntries.length > 0
      ? `${degradedEntries.length === 1 ? "1 PMO instance is not polling" : `${degradedEntries.length} PMO instances are not polling`}: ${degradedEntries.map(([n]) => n).join(", ")} — no new missions from ${degradedEntries.length === 1 ? "it" : "them"} until it recovers.`
      : pollState.last_poll_at == null
        ? `Waiting for the first PMO poll — the server polls every ~${pollState.poll_interval_seconds}s.`
        : `Last polled ${formatAgo(secondsSincePoll)} · next in ~${nextInSeconds}s`;
  const cadenceClass =
    degradedEntries.length > 0
      ? "text-xs text-red-700 dark:text-red-300"
      : "text-xs text-neutral-500 dark:text-neutral-400";

  return (
    <div className="space-y-4">
      <PageHeader
        title="Missions"
        subtitle="Every DevCake mission across your configured PMOs — the PMO is the source of truth; click a mission to open its drawer"
        actions={
          <span className="flex items-center gap-2">
            {/* one visually primary action per header (DESIGN.md §3): with a
                composer, creating work is the thing the operator comes to
                do; Poll now stays visible as a ghost — a cadence nudge the
                cadence line already narrates, never buried in a menu */}
            <Button
              kind="ghost"
              icon={RefreshCw}
              onClick={doPoll}
              disabled={pollBusy}
              title="Force a PMO poll cycle now (the server polls automatically every ~30s)"
            >
              {pollBusy ? "Polling…" : "Poll now"}
            </Button>
            <Button icon={Plus} onClick={() => setComposerOpen(true)}>
              New mission
            </Button>
          </span>
        }
      />
      <p className={cadenceClass} aria-live="polite">
        {cadenceLine}
      </p>
      {flash && (
        <p className="rounded-card border border-accent-200 bg-accent-50 px-3 py-2 text-sm text-accent-800 dark:border-accent-900 dark:bg-accent-950/60 dark:text-accent-200">
          {flash}
        </p>
      )}
      {error && (
        <p className="rounded-card border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
          {error}
        </p>
      )}
      {rows.length === 0 && !error && (
        <p className="rounded-card border border-neutral-200 bg-surface-raised px-4 py-6 text-center text-sm text-neutral-500 dark:border-neutral-800 dark:bg-surface-raised-dark dark:text-neutral-400">
          No missions yet — waiting for the first PMO poll. Missions live in your PMO;
          create one there — or right here with New mission — and DevCake picks it
          up on the next cycle.
        </p>
      )}
      {/* Pipeline strip + grouped list (2026-08-02 board re-decision,
          DESIGN.md §2): kanban geometry assumed even occupancy, but the
          steady state is extreme skew — working stages drain by design while
          Done accumulates — so six empty columns burned ~86% of the width
          and 4 clipped cards represented a 30-mission fleet. The strip keeps
          the pipeline shape at a glance; the list spends the width on what
          varies: many missions × long titles. Works at ANY viewport width —
          the sidebar force-collapse exception died with the columns. */}
      {rows.length > 0 && (
        <div
          data-testid="pipeline-strip"
          className="sticky top-0 z-10 -mx-4 flex flex-wrap items-center gap-1.5 bg-surface/90 px-4 py-2 backdrop-blur dark:bg-surface-dark/90"
        >
          {COLUMNS.map((col) => {
            const n = buckets[col.id].length;
            if (n === 0) {
              return (
                <span
                  key={col.id}
                  className="rounded-full border border-neutral-200 px-2.5 py-1 text-xs text-neutral-400 dark:border-neutral-800 dark:text-neutral-600"
                >
                  {col.label} <span className="tabular-nums">0</span>
                </span>
              );
            }
            const hot = col.id === "needs_human";
            if (hiddenStages.includes(col.id)) {
              // hidden stage: the pill stays, dimmed, with the LIVE count —
              // never invisible-invisible; clicking shows the section again
              return (
                <button
                  key={col.id}
                  type="button"
                  aria-label={`${col.label}: ${n} — hidden, click to show`}
                  onClick={() => toggleStage(col.id)}
                  className={`rounded-full border border-dashed px-2.5 py-1 text-xs transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 ${
                    hot
                      ? "border-amber-300 text-amber-700/70 hover:bg-amber-50 dark:border-amber-800 dark:text-amber-300/70 dark:hover:bg-amber-950/40"
                      : "border-neutral-300 text-neutral-400 hover:bg-stone-50 dark:border-neutral-700 dark:text-neutral-500 dark:hover:bg-neutral-900"
                  }`}
                >
                  {col.label} <span className="tabular-nums">{n}</span>
                </button>
              );
            }
            return (
              <button
                key={col.id}
                type="button"
                aria-label={`${col.label}: ${n} — jump to section`}
                onClick={() =>
                  document.getElementById(`stage-${col.id}`)
                    ?.scrollIntoView({ behavior: "smooth", block: "start" })}
                className={`rounded-full border px-2.5 py-1 text-xs font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 ${
                  hot
                    ? "border-amber-300 bg-amber-50 text-amber-800 hover:bg-amber-100 dark:border-amber-800 dark:bg-amber-950/60 dark:text-amber-300"
                    : "border-accent-300 bg-accent-50 text-accent-800 hover:bg-accent-100 dark:border-accent-800 dark:bg-accent-950/60 dark:text-accent-200"
                }`}
              >
                {col.label} <span className="font-semibold tabular-nums">{n}</span>
              </button>
            );
          })}
          {multiPmo && (
            <span className="ml-auto flex shrink-0 items-center gap-2">
              {activeFilter && (
                <span className="text-xs tabular-nums text-neutral-500 dark:text-neutral-400">
                  {filteredRows.length} of {rows.length} missions
                </span>
              )}
              <span className="w-36 shrink-0">
                <Select
                  aria-label="Filter missions by PMO instance"
                  value={activeFilter}
                  onChange={(e) => setPmoFilter(e.target.value)}
                >
                  <option value="">all PMOs</option>
                  {teamNames.sort().map((name) => (
                    <option key={name} value={name} title={`${name} — team ${data.teams[name]}`}>
                      {name}
                    </option>
                  ))}
                </Select>
              </span>
            </span>
          )}
        </div>
      )}
      {activeFilter && filteredRows.length === 0 && rows.length > 0 && (
        // dedicated filtered-empty copy — never the bootstrap empty state,
        // which would lie about an empty fleet
        <div className="rounded-card border border-neutral-200 bg-surface-raised px-4 py-6 text-center text-sm text-neutral-500 dark:border-neutral-800 dark:bg-surface-raised-dark dark:text-neutral-400">
          <p>
            No missions from {activeFilter} — {rows.length}{" "}
            {rows.length === 1 ? "mission" : "missions"} from other PMOs{" "}
            {rows.length === 1 ? "is" : "are"} hidden by this filter.
          </p>
          <button
            type="button"
            onClick={() => setPmoFilter("")}
            className="mt-2 rounded text-xs text-accent-700 underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:text-accent-300"
          >
            Show all PMOs
          </button>
        </div>
      )}
      <div data-testid="mission-list" className="space-y-4 pb-4">
        {SECTION_ORDER.filter(
          (id) => buckets[id].length > 0 && !hiddenStages.includes(id),
        ).map((id) => {
          const col = COLUMNS.find((c) => c.id === id);
          const bucket = buckets[id];
          const shared = sharedReason(bucket);
          const shown =
            id === "done" && !showAllDone ? bucket.slice(0, DONE_PREVIEW) : bucket;
          return (
            <section
              key={id}
              id={`stage-${id}`}
              aria-label={col.label}
              className="scroll-mt-12 overflow-hidden rounded-card border border-neutral-200 bg-surface-raised shadow-card dark:border-neutral-800 dark:bg-surface-raised-dark"
            >
              <header className="flex items-center gap-2 border-b border-neutral-100 px-3 py-2 dark:border-neutral-800">
                <span
                  className={`text-xs font-semibold uppercase tracking-wide ${
                    id === "needs_human"
                      ? "text-amber-700 dark:text-amber-300"
                      : "text-neutral-500 dark:text-neutral-400"
                  }`}
                >
                  {col.label}
                </span>
                <span className="text-xs tabular-nums text-neutral-500 dark:text-neutral-400">
                  {bucket.length}
                </span>
                {shared && (
                  <span
                    className="min-w-0 truncate text-[11px] text-neutral-400 dark:text-neutral-500"
                    title={shared}
                  >
                    — {shared}
                  </span>
                )}
                <span className="ml-auto flex shrink-0 items-center gap-3">
                  {id === "done" && bucket.length > DONE_PREVIEW && (
                    <button
                      type="button"
                      onClick={() => setShowAllDone((v) => !v)}
                      className="rounded text-xs text-accent-700 underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:text-accent-300"
                    >
                      {showAllDone ? "Show fewer" : `Show all ${bucket.length}`}
                    </button>
                  )}
                  <button
                    type="button"
                    aria-label={`Hide the ${col.label} section`}
                    title="Hide this stage from the list — its strip pill keeps the count"
                    onClick={() => toggleStage(id)}
                    className="rounded text-xs text-neutral-400 underline underline-offset-2 hover:text-neutral-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:text-neutral-500 dark:hover:text-neutral-300"
                  >
                    Hide
                  </button>
                </span>
              </header>
              <div>
                {shown.map((row) => (
                  <MissionRow
                    // instance-qualified: gitea_issues pmo_ids are per-repo
                    // integers (ADR-0009), so bare pmo_id collides across a
                    // multi-gitea fleet
                    key={`${row.instance}:${row.pmo_id}`}
                    row={row}
                    multiPmo={multiPmo}
                    syncing={!!pending[row.pmo_id]?.syncing}
                    sectionReason={shared}
                    onOpen={() => setOpenMission(row)}
                    onAction={(action) => requestAction(row.pmo_id, action)}
                  />
                ))}
              </div>
            </section>
          );
        })}
      </div>
      {openMission && (
        <MissionDrawer
          // key forces a full remount when chain navigation swaps the
          // mission — a half-typed guidance draft must never post to the
          // newly opened mission
          key={`${openMission.instance}:${openMission.pmo_id}`}
          mission={openMission}
          multiPmo={multiPmo}
          syncing={!!pending[openMission.pmo_id]?.syncing}
          rows={rows}
          adoptionMode={data.adoption_mode}
          cycles={pollState.dependency_cycles}
          onOpenMission={(row) => setOpenMission(row)}
          onClose={() => setOpenMission(null)}
          onAction={(action) => requestAction(openMission.pmo_id, action)}
        />
      )}
      {confirmAction && (
        <ConfirmDialog
          open
          title={CONFIRM_COPY[confirmAction.action].title}
          body={CONFIRM_COPY[confirmAction.action].body}
          confirmLabel={CONFIRM_COPY[confirmAction.action].confirmLabel}
          busy={confirmBusy}
          onConfirm={confirmProceed}
          onCancel={() => !confirmBusy && setConfirmAction(null)}
        />
      )}
      {composerOpen && (
        <NewMissionDialog
          adoptionMode={data.adoption_mode}
          onClose={() => setComposerOpen(false)}
          onCreated={onMissionCreated}
        />
      )}
    </div>
  );
}
