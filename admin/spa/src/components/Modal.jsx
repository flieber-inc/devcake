import React, { useEffect, useRef, useState } from "react";
import Button from "./Button.jsx";

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Shared overlay: dialog semantics, focus trap, scroll lock on <main>.
// `onDismiss` enables Esc + backdrop close — omit it for confirms, which
// must be resolved by their explicit buttons only. `surfaceClass` swaps the
// default white-card surface wholesale (border + background) — pass it
// instead of stacking conflicting utilities in `className` (Tailwind
// resolves duplicate properties by stylesheet order, not class order).
export function Overlay({ children, className = "", surfaceClass, onDismiss, ariaLabel }) {
  const ref = useRef(null);
  useEffect(() => {
    const node = ref.current;
    const prev = document.activeElement;
    (node.querySelector(FOCUSABLE) || node).focus();
    const onKey = (e) => {
      if (e.key === "Tab") {
        const f = [...node.querySelectorAll(FOCUSABLE)];
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
        else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
      }
    };
    node.addEventListener("keydown", onKey);
    // the app scrolls in <main>, not body — lock that
    const main = document.querySelector("main");
    const overflow = main ? main.style.overflow : "";
    if (main) main.style.overflow = "hidden";
    return () => {
      node.removeEventListener("keydown", onKey);
      if (main) main.style.overflow = overflow;
      prev && prev.focus && prev.focus();
    };
  }, []);
  useEffect(() => {
    if (!onDismiss) return;
    const esc = (e) => e.key === "Escape" && onDismiss();
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onDismiss]);
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-[2px]"
      onClick={onDismiss ? (e) => e.target === e.currentTarget && onDismiss() : undefined}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel}
        tabIndex={-1}
        className={`w-full rounded-card shadow-2xl focus:outline-none ${
          surfaceClass || "border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900"
        } ${className}`}
      >
        {children}
      </div>
    </div>
  );
}

// No Escape/backdrop close on purpose: confirms guard consequential actions,
// so only the explicit buttons may resolve the dialog. `children` renders
// between body and buttons (e.g. an apply-preview diff); `error` keeps the
// dialog open with the failure visible so the operator can retry or cancel.
// confirmKind: destructive confirms keep the red default; a CREATIVE confirm
// (the mission composer, ADR-0030) passes "primary" — a red "Create mission"
// button would mislabel the action as destructive (§7 honesty).
export function ConfirmDialog({ open, title, body, confirmLabel, busy, error,
                                children, onConfirm, onCancel,
                                confirmKind = "danger" }) {
  if (!open) return null;
  return (
    <Overlay className="max-w-lg p-6">
      <h4 className="mb-2 text-base font-semibold tracking-tight">{title}</h4>
      <p className="mb-4 whitespace-pre-line text-sm text-neutral-600 dark:text-neutral-300">
        {body}
      </p>
      {children}
      {error && (
        <p className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300">
          {error}
        </p>
      )}
      <div className="mt-5 flex justify-end gap-2">
        <Button kind="ghost" disabled={busy} onClick={onCancel}>Cancel</Button>
        <Button kind={confirmKind} disabled={busy} onClick={onConfirm}>
          {busy ? "Working…" : confirmLabel}
        </Button>
      </div>
    </Overlay>
  );
}

// One-input dialog — the in-app replacement for window.prompt().
export function PromptDialog({
  open, title, label, initial = "", hint, placeholder,
  confirmLabel = "Save", busy, error, onConfirm, onCancel,
}) {
  const [value, setValue] = useState(initial);
  useEffect(() => { if (open) setValue(initial); }, [open, initial]);
  if (!open) return null;
  const submit = () => value.trim() && !busy && onConfirm(value.trim());
  return (
    <Overlay className="max-w-lg p-6" onDismiss={onCancel}>
      <h4 className="mb-3 text-base font-semibold tracking-tight">{title}</h4>
      <label className="block text-sm">
        <span className="mb-1 block font-medium">{label}</span>
        <input
          autoFocus
          value={value}
          placeholder={placeholder}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          className="w-full rounded-md border border-neutral-300 bg-white px-2.5 py-1.5 text-sm transition focus:border-accent-400 focus:outline-none focus:ring-2 focus:ring-accent-500/50 dark:border-neutral-700 dark:bg-neutral-950"
        />
        {hint && <span className="mt-1 block text-xs text-neutral-500 dark:text-neutral-400">{hint}</span>}
      </label>
      {error && (
        <p className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300">
          {error}
        </p>
      )}
      <div className="mt-5 flex justify-end gap-2">
        <Button kind="ghost" disabled={busy} onClick={onCancel}>Cancel</Button>
        <Button disabled={busy || !value.trim()} onClick={submit}>
          {busy ? "Working…" : confirmLabel}
        </Button>
      </div>
    </Overlay>
  );
}

export function Modal({ children, className = "max-w-lg", onClose }) {
  return (
    <Overlay className={`p-6 ${className}`} onDismiss={onClose}>
      {children}
    </Overlay>
  );
}
