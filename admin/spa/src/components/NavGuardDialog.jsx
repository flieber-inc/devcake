import React from "react";
import Button from "./Button.jsx";

// Shown when navigating away from Config with a dirty draft.
export default function NavGuardDialog({ open, count, onStay, onDiscard, onSave }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-[2px]">
      <div className="w-full max-w-lg rounded-card border border-neutral-200 bg-white p-6 shadow-2xl dark:border-neutral-800 dark:bg-neutral-900">
        <h4 className="mb-2 text-base font-semibold tracking-tight">Unsaved changes</h4>
        <p className="mb-5 text-sm text-neutral-600 dark:text-neutral-300">
          You have {count} unsaved change{count > 1 ? "s" : ""} on this page.
          Leaving without saving discards them.
        </p>
        <div className="flex flex-wrap justify-end gap-2">
          <Button kind="ghost" onClick={onStay}>Stay</Button>
          <Button kind="danger-ghost" onClick={onDiscard}>Discard &amp; leave</Button>
          <Button onClick={onSave}>Save &amp; leave…</Button>
        </div>
      </div>
    </div>
  );
}
