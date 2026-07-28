import React from "react";
import { CircleCheck, TriangleAlert } from "lucide-react";
import Button from "./Button.jsx";

// Floating footer toast for the Config draft: appears while dirty, morphs
// into a transient success state after a full save. Fixed to the viewport
// bottom — sticky only pins once content overflows, so on short pages the
// bar sat glued under the section instead of floating. The in-flow spacer
// keeps the last content reachable when scrolled to the end. Content-sized
// (w-fit); the amber dot is the same pending signal as the roster tiles'
// "unsaved changes" badge.
const FLOAT = "fixed inset-x-4 bottom-4 z-30 mx-auto flex w-fit max-w-full";
const SPACER = <div aria-hidden className="h-24 sm:h-16" />;

export default function DirtyBar({ count, errors, saved, onDiscard, onSave }) {
  if (saved) {
    return (
      <>
        {SPACER}
        <div className={`${FLOAT} items-center gap-2 rounded-full border border-green-200 bg-green-50 px-5 py-2.5 text-sm font-medium text-green-800 shadow-lg dark:border-green-900 dark:bg-green-950 dark:text-green-200`}>
          <CircleCheck size={16} aria-hidden />
          All changes saved
        </div>
      </>
    );
  }
  if (count === 0) return null;
  const errorList = Object.values(errors || {});
  return (
    <>
    {SPACER}
    <div className={`${FLOAT} flex-col gap-1.5 rounded-card border border-neutral-300 bg-white/95 py-2 pl-4 pr-2 shadow-xl backdrop-blur dark:border-neutral-700 dark:bg-neutral-900/95`}>
      <div className="flex max-w-full flex-wrap items-center justify-center gap-x-4 gap-y-2">
        <span className="flex items-center gap-2 whitespace-nowrap text-sm font-semibold">
          <span className="h-2 w-2 shrink-0 rounded-full bg-amber-500" aria-hidden />
          Unsaved changes{" "}
          <span className="font-normal tabular-nums text-neutral-500 dark:text-neutral-400">({count})</span>
        </span>
        <span className="flex items-center gap-2">
          <Button kind="ghost" onClick={onDiscard}>Discard changes</Button>
          <Button onClick={onSave} disabled={errorList.length > 0}
            title={errorList.length ? errorList.join("\n") : undefined}>
            Save changes…
          </Button>
        </span>
      </div>
      {errorList.length > 0 && (
        <p className="flex max-w-xl items-start gap-1.5 pb-1 pr-2 text-xs text-red-600 dark:text-red-400">
          <TriangleAlert size={13} className="mt-0.5 shrink-0" aria-hidden />
          <span>{errorList.join(" · ")}</span>
        </p>
      )}
    </div>
    </>
  );
}
