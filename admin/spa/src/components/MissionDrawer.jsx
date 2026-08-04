import React, { useCallback, useMemo, useState } from "react";
import { ExternalLink, Send, X } from "lucide-react";
import Button from "./Button.jsx";
import MoreMenu from "./MoreMenu.jsx";
import StageGlyph from "./StageGlyph.jsx";
import StatusPill from "./StatusPill.jsx";
import RunTerminal from "./RunTerminal.jsx";
import { Overlay, ConfirmDialog } from "./Modal.jsx";
import { Textarea } from "./Field.jsx";
import { get, send } from "../api.js";
import usePoll from "../lib/usePoll.js";
import { relTime, fullTime, duration } from "../lib/format.js";
import { contextActions, needsHumanReason } from "../lib/board.js";

const STOP_COPY = {
  title: "Stop this run?",
  body:
    "The Dev container is stopped and this run is recorded as failed.\n" +
    "It counts toward the retry limit. The mission keeps its labels and is " +
    "picked up again on a later cycle — park it first if you don't want that.",
  confirmLabel: "Stop run",
};

// Mission drawer — right-hand Overlay that groups everything you'd want from
// a Devin-style session: label chips, action row, live run history, per-run
// terminal (opens on top of the drawer — its overlay stacks OK; focus
// restores on close via the Overlay component's restore logic).
export default function MissionDrawer({ mission, multiPmo, syncing, onClose, onAction }) {
  const [runs, setRuns] = useState({ runs: [] });
  const [openRun, setOpenRun] = useState(null);
  const [comment, setComment] = useState("");
  const [posting, setPosting] = useState(false);
  const [flash, setFlash] = useState("");
  const [error, setError] = useState("");
  const [stopTarget, setStopTarget] = useState(null); // run object
  const [stopBusy, setStopBusy] = useState(false);

  const loadRuns = useCallback(async () => {
    try {
      const d = await get(
        `/runs?limit=100&mission_key=${encodeURIComponent(mission.key)}`
      );
      setRuns(d);
    } catch (e) {
      setError(String(e.message || e));
    }
  }, [mission.key]);

  usePoll(loadRuns, 10_000, [mission.key]);

  const badge = needsHumanReason(mission.labels);
  const actions = contextActions(mission);
  const menuItems = actions.map((it) => ({
    label: it.label,
    danger: it.id === "park",
    onClick: () => onAction(it.id),
  }));

  const submitComment = async () => {
    const body = comment.trim();
    if (!body || posting) return;
    setPosting(true);
    setError("");
    try {
      await send("POST", `/missions/${encodeURIComponent(mission.pmo_id)}/comment`, {
        body,
      });
      setComment("");
      setFlash(
        "Guidance sent — it appears on your PMO as a human comment, and the next poll will pick it up as steering."
      );
      setTimeout(() => setFlash(""), 5000);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setPosting(false);
    }
  };

  const confirmStop = async () => {
    if (!stopTarget) return;
    setStopBusy(true);
    try {
      await send("POST", `/runs/${encodeURIComponent(stopTarget.run_id)}/stop`);
      setStopTarget(null);
      await loadRuns();
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setStopBusy(false);
    }
  };

  // Sorted newest first (server already does, but be defensive)
  const orderedRuns = useMemo(
    () =>
      [...(runs.runs || [])].sort((a, b) =>
        (b.created_at || "").localeCompare(a.created_at || "")
      ),
    [runs]
  );

  return (
    <Overlay
      className="ml-auto h-[100dvh] max-w-2xl rounded-none border-l border-neutral-200 dark:border-neutral-800"
      onDismiss={onClose}
    >
      <div className="flex h-full flex-col">
        {/* Header */}
        <div className="flex items-start gap-3 border-b border-neutral-200 px-5 py-4 dark:border-neutral-800">
          <div className="min-w-0 grow">
            <div className="mb-1 flex items-center gap-2">
              {mission.mission_type && <StageGlyph stage={mission.mission_type} />}
              <span
                className="font-mono text-xs text-neutral-500 dark:text-neutral-400"
                title={multiPmo
                  ? `PMO instance ${mission.instance} · team ${mission.team}`
                  : undefined}
              >
                {multiPmo && (
                  <span className="text-neutral-400 dark:text-neutral-500">
                    {mission.instance}·
                  </span>
                )}
                {mission.key}
              </span>
              {syncing && (
                <span
                  title="Waiting for the next poll cycle to confirm the last change"
                  className="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-medium text-blue-800 dark:bg-blue-950 dark:text-blue-300"
                >
                  syncing…
                </span>
              )}
              {menuItems.length > 0 && (
                <MoreMenu label={`Actions for ${mission.key}`} items={menuItems} />
              )}
              <span className="grow" />
              {mission.url && (
                <a
                  href={mission.url}
                  target="_blank"
                  rel="noopener"
                  className="inline-flex items-center gap-1 text-xs text-accent-700 underline underline-offset-2 dark:text-accent-300"
                >
                  Open in PMO <ExternalLink size={11} aria-hidden />
                </a>
              )}
              <button
                type="button"
                aria-label="Close drawer"
                onClick={onClose}
                className="rounded p-1 text-neutral-500 transition hover:bg-stone-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:text-neutral-400 dark:hover:bg-neutral-800"
              >
                <X size={16} aria-hidden />
              </button>
            </div>
            <h2 className="mb-2 line-clamp-2 text-base font-semibold" title={mission.title}>
              {mission.title}
            </h2>
            <div className="flex flex-wrap gap-1.5">
              {(mission.labels || []).map((label) => (
                <span
                  key={label}
                  className="rounded bg-stone-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300"
                >
                  {label}
                </span>
              ))}
            </div>
            {badge && (
              <p className="mt-2 text-[11px] font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-300">
                {badge}
              </p>
            )}
            {mission.reason && (
              <p className="mt-1 text-xs text-neutral-500 dark:text-neutral-400">
                {mission.reason}
              </p>
            )}
          </div>
        </div>

        {/* Scrollable body */}
        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-4">
          {flash && (
            <p className="rounded-md border border-accent-200 bg-accent-50 px-3 py-2 text-xs text-accent-800 dark:border-accent-900 dark:bg-accent-950/60 dark:text-accent-200">
              {flash}
            </p>
          )}
          {error && (
            <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
              {error}
            </p>
          )}

          {/* Steering composer */}
          <section>
            <h3 className="mb-1.5 text-sm font-semibold">Send guidance</h3>
            <p className="mb-2 text-xs text-neutral-500 dark:text-neutral-400">
              This posts a human comment on the PMO issue. The next poll picks it up as
              🧑 HUMAN context, and the attempt counter resets.
            </p>
            <Textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder="Type direction for the Dev — visible on your PMO."
              rows={3}
              aria-label="Steering comment"
            />
            <div className="mt-2 flex items-center justify-end gap-2">
              <span className="text-xs text-neutral-500 dark:text-neutral-400">
                {comment.trim().length} chars
              </span>
              <Button
                size="sm"
                icon={Send}
                disabled={posting || !comment.trim()}
                onClick={submitComment}
              >
                {posting ? "Sending…" : "Send guidance"}
              </Button>
            </div>
          </section>

          {/* Runs */}
          <section>
            <h3 className="mb-2 text-sm font-semibold">
              Runs{" "}
              <span className="text-xs font-normal text-neutral-500 dark:text-neutral-400">
                ({runs.total ?? orderedRuns.length})
              </span>
            </h3>
            {orderedRuns.length === 0 ? (
              <p className="rounded-md border border-dashed border-neutral-200 px-3 py-6 text-center text-xs text-neutral-500 dark:border-neutral-800 dark:text-neutral-400">
                No runs yet for this mission.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[36rem] text-left text-sm">
                  <thead className="text-[10px] uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
                    <tr>
                      <th className="py-1 pr-2">run</th>
                      <th className="pr-2">stage</th>
                      <th className="pr-2">state</th>
                      <th className="pr-2">started</th>
                      <th className="pr-2">duration</th>
                      <th className="pr-2">PR</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {orderedRuns.map((r) => {
                      // finalizing hides Stop too: the Dev has already
                      // exited — the backend 409s a stop there by design
                      const stopped = ["finished", "failed", "timed_out",
                        "orphaned", "finalizing"].includes(r.state);
                      return (
                        <tr
                          key={r.run_id}
                          onClick={() => setOpenRun(r)}
                          className="cursor-pointer border-t border-neutral-100 hover:bg-stone-50 dark:border-neutral-800 dark:hover:bg-neutral-900"
                          title="Open this run's terminal"
                        >
                          <td className="py-1.5 pr-2">
                            <span className="inline-flex items-center gap-1.5">
                              {r.mission_type && <StageGlyph stage={r.mission_type} />}
                              <span className="font-mono text-[11px]">{r.run_id}</span>
                            </span>
                          </td>
                          <td className="pr-2 text-xs">{r.mission_type || "—"}</td>
                          <td className="pr-2">
                            <StatusPill state={r.state} verdict={r.verdict} />
                          </td>
                          <td
                            className="whitespace-nowrap pr-2 text-xs text-neutral-500 dark:text-neutral-400"
                            title={fullTime(r.started_at)}
                          >
                            {relTime(r.started_at)}
                          </td>
                          <td className="whitespace-nowrap pr-2 text-xs tabular-nums text-neutral-500 dark:text-neutral-400">
                            {duration(r.started_at, r.ended_at)}
                          </td>
                          <td className="pr-2">
                            {r.pr_url ? (
                              <a
                                href={r.pr_url}
                                target="_blank"
                                rel="noopener"
                                onClick={(e) => e.stopPropagation()}
                                className="inline-flex items-center gap-0.5 text-xs text-accent-700 underline underline-offset-2 dark:text-accent-300"
                              >
                                PR <ExternalLink size={10} aria-hidden />
                              </a>
                            ) : (
                              <span className="text-xs text-neutral-400">—</span>
                            )}
                          </td>
                          <td>
                            {!stopped && (
                              <Button
                                size="sm"
                                kind="danger-ghost"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setStopTarget(r);
                                }}
                              >
                                Stop
                              </Button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>

        <div className="border-t border-neutral-200 px-5 py-3 text-[11px] text-neutral-500 dark:border-neutral-800 dark:text-neutral-400">
          Board refreshes from your PMO every ~30s. Changes appear as
          “syncing…” until the next cycle confirms.
        </div>
      </div>
      {openRun && <RunTerminal run={openRun} onClose={() => setOpenRun(null)} />}
      {stopTarget && (
        <ConfirmDialog
          open
          title={STOP_COPY.title}
          body={STOP_COPY.body}
          confirmLabel={STOP_COPY.confirmLabel}
          busy={stopBusy}
          onConfirm={confirmStop}
          onCancel={() => !stopBusy && setStopTarget(null)}
        />
      )}
    </Overlay>
  );
}
