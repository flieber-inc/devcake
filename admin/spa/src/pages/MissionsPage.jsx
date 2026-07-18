import React, { useCallback, useMemo, useState } from "react";
import { Plus } from "lucide-react";
import PageHeader from "../components/PageHeader.jsx";
import Button from "../components/Button.jsx";
import MissionCard from "../components/MissionCard.jsx";
import MissionDrawer from "../components/MissionDrawer.jsx";
import NewMissionDialog from "../components/NewMissionDialog.jsx";
import { ConfirmDialog } from "../components/Modal.jsx";
import { get, send } from "../api.js";
import usePoll from "../lib/usePoll.js";
import { bucketize, COLUMNS } from "../lib/board.js";

// Board refresh cadence — the /missions endpoint reflects the last poll
// cycle (~30s at the server); polling at 10s here keeps the UI fresh without
// leading the operator to expect a faster PMO round-trip than exists.
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

export default function MissionsPage() {
  const [data, setData] = useState({ missions: [], adoption_mode: "auto", teams: {} });
  const [error, setError] = useState("");
  // per-mission optimistic overrides (pmo_id → { labels, syncing:true }).
  // Cleared once the next /missions poll confirms the change.
  const [pending, setPending] = useState({});
  const [openMission, setOpenMission] = useState(null);
  const [newOpen, setNewOpen] = useState(false);
  const [confirmAction, setConfirmAction] = useState(null); // {pmo_id, action}
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [flash, setFlash] = useState("");

  const load = useCallback(async () => {
    try {
      const d = await get("/missions");
      setData(d);
      // clear pending overrides whose labels the server now reflects
      setPending((prev) => {
        const next = {};
        for (const [pmo_id, entry] of Object.entries(prev)) {
          const row = d.missions.find((m) => m.pmo_id === pmo_id);
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

  // Merge server rows with any optimistic overrides so cards flip immediately.
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
      // syncing until the next poll shows the projected labels
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

  const instanceOptions = Object.entries(data.teams || {}).map(([name, team_key]) => ({
    name,
    team_key,
  }));

  return (
    <div className="space-y-4">
      <PageHeader
        title="Missions"
        subtitle="Every DevCake mission across your configured PMOs — click a card to open its drawer"
        actions={
          <Button icon={Plus} onClick={() => setNewOpen(true)}>
            New mission
          </Button>
        }
      />
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
          No missions yet — waiting for the first PMO poll.
        </p>
      )}
      <div className="overflow-x-auto pb-4">
        <div className="flex min-w-max gap-3">
          {COLUMNS.map((col) => (
            <section
              key={col.id}
              aria-label={col.label}
              className="flex w-72 shrink-0 flex-col rounded-card border border-neutral-200 bg-stone-50 p-2 dark:border-neutral-800 dark:bg-neutral-950/40"
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
      {newOpen && (
        <NewMissionDialog
          instances={instanceOptions}
          onClose={() => setNewOpen(false)}
          onCreated={async () => {
            setNewOpen(false);
            await load();
          }}
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
