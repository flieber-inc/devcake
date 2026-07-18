import React from "react";
import Button from "./Button.jsx";
import { Overlay } from "./Modal.jsx";

// Shown when navigating away from Config with a dirty draft.
// Esc / backdrop = "Stay" (the safe choice — nothing is lost).
export default function NavGuardDialog({ open, count, errors, onStay, onDiscard, onSave }) {
  if (!open) return null;
  const errorList = Object.values(errors || {});
  return (
    <Overlay className="max-w-lg p-6" onDismiss={onStay}>
        <h4 className="mb-2 text-base font-semibold tracking-tight">Unsaved changes</h4>
        <p className="mb-5 text-sm text-neutral-600 dark:text-neutral-300">
          You have {count} unsaved change{count > 1 ? "s" : ""} on this page.
          Leaving without saving discards them.
        </p>
        {errorList.length > 0 && (
          <p className="mb-4 text-xs text-red-600 dark:text-red-400">
            Saving is blocked until fixed: {errorList.join(" · ")}
          </p>
        )}
        <div className="flex flex-wrap justify-end gap-2">
          <Button kind="ghost" onClick={onStay}>Stay</Button>
          <Button kind="danger-ghost" onClick={onDiscard}>Discard &amp; leave</Button>
          <Button onClick={onSave} disabled={errorList.length > 0}
            title={errorList.length ? errorList.join("\n") : undefined}>
            Save &amp; leave…
          </Button>
        </div>
    </Overlay>
  );
}
