import React, { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import PageHeader from "../components/PageHeader.jsx";
import Button from "../components/Button.jsx";
import MissionRow from "../components/MissionRow.jsx";
import MissionDrawer from "../components/MissionDrawer.jsx";
import { ConfirmDialog } from "../components/Modal.jsx";
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
  });
  const [error, setError] = useState("");
  // per-mission optimistic overrides (pmo_id → { labels, syncing:true }).
  // Cleared once the next /missions poll confirms the change.
  const [pending, setPending] = useState({});
  const [openMission, setOpenMission] = useState(null);
  const [showAllDone, setShowAllDone] = useState(false);
  const [confirmAction, setConfirmAction] = useState(null); // {pmo_id, action}
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [flash, setFlash] = useState("");
  const [pollBusy, setPollBusy] = useState(false);
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

  const buckets = useMemo(
    () => bucketize(rows, data.adoption_mode),
    [rows, data.adoption_mode]
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
          <Button
            icon={RefreshCw}
            onClick={doPoll}
            disabled={pollBusy}
            title="Force a PMO poll cycle now (the server polls automatically every ~30s)"
          >
            {pollBusy ? "Polling…" : "Poll now"}
          </Button>
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
          No missions yet — waiting for the first PMO poll. Missions are born in your PMO;
          create one there and DevCake will pick it up on the next cycle.
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
        </div>
      )}
      <div data-testid="mission-list" className="space-y-4 pb-4">
        {SECTION_ORDER.filter((id) => buckets[id].length > 0).map((id) => {
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
                {id === "done" && bucket.length > DONE_PREVIEW && (
                  <button
                    type="button"
                    onClick={() => setShowAllDone((v) => !v)}
                    className="ml-auto shrink-0 rounded text-xs text-accent-700 underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:text-accent-300"
                  >
                    {showAllDone ? "Show fewer" : `Show all ${bucket.length}`}
                  </button>
                )}
              </header>
              <div>
                {shown.map((row) => (
                  <MissionRow
                    key={row.pmo_id}
                    row={row}
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
          mission={openMission}
          syncing={!!pending[openMission.pmo_id]?.syncing}
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
    </div>
  );
}
