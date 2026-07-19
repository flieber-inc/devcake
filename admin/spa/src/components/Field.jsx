import React, { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { get, send } from "../api.js";
import { ConfirmDialog } from "./Modal.jsx";
import { getRegistry } from "../lib/registry.js";

const inputCls =
  "w-full rounded-md border border-neutral-300 bg-white px-2.5 py-1.5 text-sm " +
  "transition focus:outline-none focus:ring-2 focus:ring-accent-500/50 focus:border-accent-400 " +
  "dark:border-neutral-700 dark:bg-neutral-950";

export function Input({ className = "", ...props }) {
  return <input className={`${inputCls} ${className}`} {...props} />;
}

export function Select({ className = "", children, ...props }) {
  return <select className={`${inputCls} ${className}`} {...props}>{children}</select>;
}

export function Textarea({ className = "", ...props }) {
  return <textarea className={`${inputCls} ${className}`} {...props} />;
}

// List-editing textarea: the draft holds the CLEAN parsed list (save/diff
// machinery untouched) while the textarea renders local raw text. The old
// value={list.join("\n")} + onChange split().filter(Boolean) round-trip
// erased the trailing newline on every re-render, so Enter did nothing and
// two typed lines silently concatenated into one entry.
export function ListTextarea({ value, onChange, ...props }) {
  const parse = (t) => t.split("\n").map((s) => s.trim()).filter(Boolean);
  const [raw, setRaw] = useState((value || []).join("\n"));
  useEffect(() => {
    // external draft change (Discard, rebase) → resync the local text
    if (JSON.stringify(parse(raw)) !== JSON.stringify(value || []))
      setRaw((value || []).join("\n"));
  }, [value]);
  return (
    <Textarea
      {...props}
      value={raw}
      onChange={(e) => {
        setRaw(e.target.value);
        onChange(parse(e.target.value));
      }}
    />
  );
}

// Click-to-toggle help popover: a real button, so it works on touch and by
// keyboard. Fixed positioning keeps it inside drawers and narrow form cards.
export function Help({ text }) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 8, top: 8, maxHeight: 320 });
  const id = useId();
  const ref = useRef(null);
  const buttonRef = useRef(null);
  const tooltipRef = useRef(null);
  const place = () => {
    const trigger = buttonRef.current?.getBoundingClientRect();
    const tooltip = tooltipRef.current;
    if (!trigger || !tooltip) return;
    const gutter = 8;
    const gap = 6;
    const width = tooltip.offsetWidth || Math.min(256, innerWidth - gutter * 2);
    const maxHeight = Math.max(96, innerHeight - gutter * 2);
    const height = Math.min(tooltip.scrollHeight, maxHeight);
    const left = Math.min(
      Math.max(gutter, trigger.left + trigger.width / 2 - width / 2),
      Math.max(gutter, innerWidth - width - gutter),
    );
    const below = trigger.bottom + gap;
    const top = below + height <= innerHeight - gutter
      ? below
      : Math.max(gutter, trigger.top - height - gap);
    setPosition({ left, top, maxHeight });
  };
  useLayoutEffect(() => {
    if (!open) return;
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const away = (e) => ref.current && !ref.current.contains(e.target) && setOpen(false);
    const esc = (e) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
      buttonRef.current?.focus();
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc, true);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", esc, true);
    };
  }, [open]);
  return (
    <span ref={ref} className="relative ml-1 inline-block align-middle">
      <button
        ref={buttonRef}
        type="button"
        aria-label="Help"
        aria-expanded={open}
        aria-describedby={open ? id : undefined}
        onClick={(e) => { e.preventDefault(); setOpen(!open); }}
        className="flex h-6 w-6 items-center justify-center rounded-full bg-neutral-200 text-[10px] font-semibold text-neutral-600 hover:bg-neutral-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:bg-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-600 sm:h-4 sm:w-4"
      >
        ?
      </button>
      {open && (
        <span
          ref={tooltipRef}
          id={id}
          role="tooltip"
          style={position}
          className="fixed z-50 w-64 max-w-[calc(100vw-1rem)] overflow-y-auto overscroll-contain rounded-md bg-neutral-950 p-2 text-left text-xs font-normal leading-relaxed text-neutral-100 shadow-lg dark:bg-neutral-800"
        >
          {text}
        </span>
      )}
    </span>
  );
}

// A <label> forwards clicks to its first labelable descendant, so wrapping
// arbitrary children (chips, toggles) made the heading text itself toggle
// the first control. The heading is a real <label htmlFor> only when the
// child is a single plain input; everything else gets an inert <span>.
export function Field({ label, hint, help, children }) {
  const id = useId();
  const single =
    React.isValidElement(children) &&
    [Input, Select, Textarea, ListTextarea].includes(children.type);
  const Heading = single ? "label" : "span";
  return (
    <div className="block text-sm">
      <Heading {...(single ? { htmlFor: id } : {})} className="mb-1 block font-medium">
        {label}
        {help && <Help text={help} />}
      </Heading>
      {single ? React.cloneElement(children, { id }) : children}
      {hint && <span className="mt-1 block text-xs text-neutral-500 dark:text-neutral-400">{hint}</span>}
    </div>
  );
}

// ── secret-shape detection (the token_env incident, 2026-07-11) ─────────────
// harness key shapes stay static; PMO/forge token prefixes come from the
// adapter registry so a new adapter's secrets are guarded without SPA edits
const GENERIC_SECRET_PREFIXES = ["sk-", "xai-"];

function secretShapeRe() {
  const prefixes = [...GENERIC_SECRET_PREFIXES,
                    ...getRegistry().secret_shape_prefixes];
  const escaped = prefixes.map((p) => p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  return new RegExp(`^(${escaped.join("|")})`);
}

// Write-only secret VALUE field (schema v4, F5). The value is never fetched
// back; ✓/✗ + updated_at come from /secrets-check by ref. Typing a value and
// blurring (or clicking Save) PUTs it to the store — the input then clears.
// `optional`: an absent value is a fine steady state — show a neutral note
// instead of the amber "enter a value" (which reads as REQUIRED and made the
// three repo token fields look all-mandatory). `absentNote` overrides the
// amber copy for required-ish fields with a more precise consequence.
export function SecretField({ label, help, hint, refKey, checkKind = "conn",
                              paste, locked, optional, absentNote }) {
  // refKey: "scope:instance:field" for connections, or a var name for harness
  const [status, setStatus] = useState(null);      // {present, updated_at}
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [askRemove, setAskRemove] = useState(false);
  const refresh = () => {
    const q = checkKind === "harness"
      ? `harness=${encodeURIComponent(refKey)}`
      : `conn=${encodeURIComponent(refKey)}`;
    get(`/secrets-check?${q}`)
      .then((r) => setStatus((r[checkKind] || {})[refKey] || { present: false }))
      .catch(() => setStatus(null));
  };
  useEffect(refresh, [refKey]);
  const submit = async () => {
    if (!draft) return;
    setBusy(true);
    try {
      if (checkKind === "harness") {
        await send("PUT", `/harness-secrets/${encodeURIComponent(refKey)}`, { value: draft });
      } else {
        const [scope, instance, field] = refKey.split(":");
        await send("PUT", `/secrets/${scope}/${instance}/${field}`, { value: draft });
      }
      setDraft("");
      refresh();
    } finally { setBusy(false); }
  };
  const remove = async () => {
    setBusy(true);
    try {
      if (checkKind === "harness") {
        await send("DELETE", `/harness-secrets/${encodeURIComponent(refKey)}`);
      } else {
        const [scope, instance, field] = refKey.split(":");
        await send("DELETE", `/secrets/${scope}/${instance}/${field}`);
      }
      refresh();
    } finally { setBusy(false); setAskRemove(false); }
  };
  const shapeWarn = paste && draft && !secretShapeRe().test(draft) && draft.length < 8
    ? "That does not look like a secret — double-check before saving."
    : null;
  // stored secrets key on the instance name — until the instance is saved
  // the name can still change, which would orphan an already-stored value
  if (locked) {
    return (
      <Field label={label} help={help} hint={hint}>
        <Input type="password" value="" disabled aria-label={label} placeholder="save the instance first" />
        <span className="mt-1 block text-xs text-neutral-500 dark:text-neutral-400">
          Save this page to create the instance, then set its secret here.
        </span>
      </Field>
    );
  }
  return (
    <Field label={label} help={help} hint={hint}>
      <div className="flex flex-col gap-2 sm:flex-row">
        <Input type="password" value={draft} aria-label={label}
          placeholder={status?.present ? "•••••• (stored)" : "paste value"}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()} />
        <button type="button" disabled={!draft || busy}
          className="min-h-10 rounded bg-neutral-800 px-3 text-sm text-white disabled:opacity-40 dark:bg-neutral-200 dark:text-black sm:min-h-0"
          onClick={submit}>{status?.present ? "Replace" : "Set"}</button>
      </div>
      {shapeWarn && <span className="mt-1 block text-xs text-amber-600">⚠ {shapeWarn}</span>}
      {status && status.present && (
        <span className="mt-1 flex items-center gap-2 text-xs">
          <span className="text-green-700 dark:text-green-400">✓ stored</span>
          <button type="button" disabled={busy} onClick={() => setAskRemove(true)}
            className="text-red-600 underline-offset-2 hover:underline disabled:opacity-40 dark:text-red-400">
            Remove
          </button>
        </span>
      )}
      <ConfirmDialog open={askRemove}
        title={`Remove the stored value for ${label}?`}
        body="Runs that need it will fail until a new one is set."
        confirmLabel="Remove" busy={busy}
        onConfirm={remove} onCancel={() => setAskRemove(false)} />
      {status && !status.present && (
        optional
          ? <span className="mt-1 block text-xs text-neutral-500 dark:text-neutral-400">not set (optional)</span>
          : <span className="mt-1 block text-xs text-amber-600 dark:text-amber-400">
              ✗ {absentNote || "not set — enter a value"}
            </span>
      )}
    </Field>
  );
}
