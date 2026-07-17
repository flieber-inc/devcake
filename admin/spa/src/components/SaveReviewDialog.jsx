import React from "react";
import { TriangleAlert, CircleCheck, CircleX } from "lucide-react";
import Button from "./Button.jsx";
import { Overlay } from "./Modal.jsx";
import { GROUP_ORDER } from "../lib/configLabels.js";

function Row({ row }) {
  return (
    <div className="py-1.5 text-sm">
      <span className="font-medium">{row.label}</span>
      {row.multiline ? (
        <div className="mt-1 grid gap-2 sm:grid-cols-2">
          <pre className="overflow-x-auto rounded bg-stone-50 p-2 text-xs text-neutral-500 line-through decoration-neutral-300 dark:bg-neutral-800/60 dark:text-neutral-400">{row.oldText}</pre>
          <pre className="overflow-x-auto rounded bg-stone-50 p-2 text-xs dark:bg-neutral-800/60">{row.newText}</pre>
        </div>
      ) : (
        <span className="ml-2 text-neutral-500 dark:text-neutral-400">
          <span className="line-through decoration-neutral-300">{row.oldText}</span>
          <span className="mx-1.5">→</span>
          <span className="font-medium text-neutral-900 dark:text-neutral-100">{row.newText}</span>
        </span>
      )}
    </div>
  );
}

// Review-and-confirm for the Config draft. Two views:
//  - review: every pending change (dangerous rows hoisted on top, amber)
//  - results: per-unit ✓/✗ after a save attempt with a partial failure
export default function SaveReviewDialog({ open, rows, busy, results, onConfirm, onCancel, onRetry }) {
  if (!open) return null;
  const warned = rows.filter((r) => r.warning);
  const plain = rows.filter((r) => !r.warning);
  const groups = GROUP_ORDER.filter((g) => plain.some((r) => r.group === g));
  const failed = results && Object.entries(results).filter(([, r]) => !r.ok);

  return (
    <Overlay className="flex max-h-[85vh] max-w-2xl flex-col"
      onDismiss={busy ? undefined : onCancel}>
        <div className="border-b border-neutral-100 px-6 py-4 dark:border-neutral-800">
          <h4 className="text-base font-semibold tracking-tight">
            {results ? "Save results" : `Review ${rows.length} change${rows.length > 1 ? "s" : ""}`}
          </h4>
        </div>

        <div className="grow overflow-y-auto px-6 py-4">
          {!results && (
            <>
              {warned.map((r) => (
                <div key={r.path}
                  className="mb-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 dark:border-amber-800 dark:bg-amber-950/60">
                  <div className="flex items-start gap-2">
                    <TriangleAlert size={15} className="mt-1 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden />
                    <div className="min-w-0">
                      <Row row={r} />
                      <p className="whitespace-pre-line pb-1 text-xs text-amber-800 dark:text-amber-300">
                        {r.warning}
                      </p>
                    </div>
                  </div>
                </div>
              ))}
              {groups.map((g) => (
                <div key={g} className="mb-2">
                  <p className="mb-0.5 text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">{g}</p>
                  <div className="divide-y divide-neutral-100 dark:divide-neutral-800">
                    {plain.filter((r) => r.group === g).map((r) => <Row key={r.path} row={r} />)}
                  </div>
                </div>
              ))}
            </>
          )}
          {results && (
            <ul className="space-y-2">
              {Object.entries(results).map(([unit, r]) => (
                <li key={unit} className="flex items-start gap-2 text-sm">
                  {r.ok
                    ? <CircleCheck size={15} className="mt-0.5 shrink-0 text-green-600" aria-hidden />
                    : <CircleX size={15} className="mt-0.5 shrink-0 text-red-600" aria-hidden />}
                  <span className="font-medium">{unit}</span>
                  {!r.ok && <span className="text-red-600 dark:text-red-400">{r.error}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-neutral-100 px-6 py-4 dark:border-neutral-800">
          {!results && (
            <>
              <Button kind="ghost" disabled={busy} onClick={onCancel}>Cancel</Button>
              <Button disabled={busy} onClick={onConfirm}>
                {busy
                  ? "Saving…"
                  : `Save ${rows.length} change${rows.length > 1 ? "s" : ""}${
                      warned.length ? ` (${warned.length} need${warned.length > 1 ? "" : "s"} attention)` : ""
                    }`}
              </Button>
            </>
          )}
          {results && (
            <>
              <Button kind="ghost" disabled={busy} onClick={onCancel}>Close</Button>
              {failed.length > 0 && (
                <Button disabled={busy} onClick={onRetry}>
                  {busy ? "Saving…" : "Retry failed"}
                </Button>
              )}
            </>
          )}
        </div>
    </Overlay>
  );
}
