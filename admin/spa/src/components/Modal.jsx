import React from "react";
import Button from "./Button.jsx";

// No Escape/backdrop close on purpose: confirms guard consequential actions,
// so only the explicit buttons may resolve the dialog.
export function ConfirmDialog({ open, title, body, confirmLabel, busy, onConfirm, onCancel }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-[2px]">
      <div className="w-full max-w-lg rounded-card border border-neutral-200 bg-white p-6 shadow-2xl dark:border-neutral-800 dark:bg-neutral-900">
        <h4 className="mb-2 text-base font-semibold tracking-tight">{title}</h4>
        <p className="mb-5 whitespace-pre-line text-sm text-neutral-600 dark:text-neutral-300">
          {body}
        </p>
        <div className="flex justify-end gap-2">
          <Button kind="ghost" disabled={busy} onClick={onCancel}>Cancel</Button>
          <Button kind="danger" disabled={busy} onClick={onConfirm}>
            {busy ? "Working…" : confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function Modal({ children, className = "" }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-[2px]">
      <div className={`w-full max-w-lg rounded-card border border-neutral-200 bg-white p-6 shadow-2xl dark:border-neutral-800 dark:bg-neutral-900 ${className}`}>
        {children}
      </div>
    </div>
  );
}
