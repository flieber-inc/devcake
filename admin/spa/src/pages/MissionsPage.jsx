import React, { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import PageHeader from "../components/PageHeader.jsx";
import Button from "../components/Button.jsx";
import MissionCard from "../components/MissionCard.jsx";
import MissionDrawer from "../components/MissionDrawer.jsx";
import { ConfirmDialog } from "../components/Modal.jsx";
import { get, send } from "../api.js";
import usePoll from "../lib/usePoll.js";
import { bucketize, COLUMNS } from "../lib/board.js";

// Board refresh cadence — /missions reflects the last PMO poll (~30s at the
// server); polling at 10s here keeps the UI fresh without leading the operator
// to expect a faster PMO round-trip than exists.
const POLL_MS = 10_000;

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
        subtitle="Every DevCake mission across your configured PMOs — the PMO is the source of truth; click a card to open its drawer"
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
        <p className="rounded-card border border-blue-200 bg-blue-50 px-3 py-2 text-sm text-blue-800 dark:border-blue-900 dark:bg-blue-950/60 dark:text-blue-200">
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
      {/* Fluid columns (max 18rem); 8rem/col floor keeps all 7 columns fitting
          without horizontal scroll down to 1280 (7×8rem + gaps < the content
          area) — overflow-x-auto is the mobile fallback below that. */}
      <div data-testid="board-scroller" className="overflow-x-auto pb-4">
        <div className="flex gap-2">
          {COLUMNS.map((col) => (
            <section
              key={col.id}
              aria-label={col.label}
              className="flex flex-1 basis-0 min-w-[8rem] max-w-[18rem] flex-col rounded-card border border-neutral-200 bg-stone-50 p-2 dark:border-neutral-800 dark:bg-neutral-950/40"
            >
              <header className="mb-2 flex items-center justify-between px-1 pb-1">
                <span className="text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
                  {col.label}
                </span>
                <span className="text-xs tabular-nums text-neutral-500 dark:text-neutral-400">
                  {buckets[col.id].length}
                </span>
              </header>
              <div className="flex flex-col gap-2">
                {buckets[col.id].length === 0 && (
                  <p className="rounded-card border border-dashed border-neutral-200 px-2 py-4 text-center text-[11px] text-neutral-400 dark:border-neutral-800">
                    empty
                  </p>
                )}
                {buckets[col.id].map((row) => (
                  <MissionCard
                    key={row.pmo_id}
                    row={row}
                    syncing={!!pending[row.pmo_id]?.syncing}
                    onOpen={() => setOpenMission(row)}
                    onAction={(action) => requestAction(row.pmo_id, action)}
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
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
