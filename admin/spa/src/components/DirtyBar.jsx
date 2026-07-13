import React from "react";
import { CircleCheck, TriangleAlert } from "lucide-react";
import Button from "./Button.jsx";

// Sticky footer toast for the Config draft: appears while dirty, morphs into
// a transient success state after a full save.
export default function DirtyBar({ count, errors, saved, onDiscard, onSave }) {
  if (saved) {
    return (
      <div className="sticky bottom-4 z-30 mx-auto flex w-fit items-center gap-2 rounded-full border border-green-200 bg-green-50 px-5 py-2.5 text-sm font-medium text-green-800 shadow-lg dark:border-green-900 dark:bg-green-950 dark:text-green-200">
        <CircleCheck size={16} aria-hidden />
        All changes saved
      </div>
    );
  }
  if (count === 0) return null;
  const errorList = Object.values(errors || {});
  return (
    <div className="sticky bottom-4 z-30 mx-auto flex w-full max-w-2xl flex-col gap-1.5 rounded-card border border-neutral-300 bg-white/95 px-4 py-3 shadow-xl backdrop-blur dark:border-neutral-700 dark:bg-neutral-900/95">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-sm font-semibold">
          Unsaved changes <span className="tabular-nums text-neutral-400">({count})</span>
        </span>
        <span className="grow" />
        <Button kind="ghost" onClick={onDiscard}>Discard changes</Button>
        <Button onClick={onSave} disabled={errorList.length > 0}
          title={errorList.length ? errorList.join("\n") : undefined}>
          Save changes…
        </Button>
      </div>
      {errorList.length > 0 && (
        <p className="flex items-start gap-1.5 text-xs text-red-600 dark:text-red-400">
          <TriangleAlert size={13} className="mt-0.5 shrink-0" aria-hidden />
          <span>{errorList.join(" · ")}</span>
        </p>
      )}
    </div>
  );
}
