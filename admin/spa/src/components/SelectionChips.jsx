import React from "react";
import { Field } from "./Field.jsx";

// The panel's standard multi-select (docs/11 "Multi-select convention"):
// ordered toggle chips over a catalog of names. Selection order = click
// order — callers for whom order is meaningless should normalize it in
// onChange (the draft diff compares arrays order-sensitively). A selected
// name whose option no longer exists renders as a red strikethrough ✕ chip:
// visible AND removable, so a stale entry can never wedge the Save PUT.
// Every new multi-select field uses this component — not checkbox lists,
// not multi-select dropdowns.
export default function SelectionChips({
  label, help,
  options,                 // [{ name, title?, disabled?, disabledNote? }]
  selected,                // ordered selected names
  onChange,                // (nextOrderedNames) => void
  emptyNote = "nothing to select yet",
  staleNote = "this entry no longer exists — click to remove it",
  firstBadge = "",         // appended to the FIRST selected chip (e.g. " · default")
  disabled = false,        // whole-control off (note rendered after the chips)
  disabledNote = "",
}) {
  const stale = selected.filter((n) => !options.some((o) => o.name === n));
  return (
    <Field label={label} help={help}>
      <div className="flex flex-wrap items-center gap-1.5 pt-1">
        {options.length === 0 && stale.length === 0 && (
          <span className="text-xs text-neutral-400">{emptyNote}</span>
        )}
        {options.map((o) => {
          const sel = selected.includes(o.name);
          const pos = selected.indexOf(o.name);
          const blocked = disabled || (!sel && o.disabled);
          const style = blocked && !sel
            ? "cursor-not-allowed border-neutral-200 text-neutral-300 dark:border-neutral-800 dark:text-neutral-600"
            : sel
              ? `border-accent-400 bg-accent-50 text-accent-800 dark:border-accent-700 dark:bg-accent-950/70 dark:text-accent-200${
                  blocked ? " cursor-not-allowed opacity-60" : ""}`
              : "border-neutral-300 text-neutral-500 hover:bg-stone-100 dark:border-neutral-700 dark:hover:bg-neutral-800";
          return (
            <button key={o.name} type="button" disabled={blocked}
              title={disabled ? undefined
                     : (!sel && o.disabled) ? o.disabledNote : o.title}
              onClick={() => onChange(
                sel ? selected.filter((n) => n !== o.name)
                    : [...selected, o.name])}
              className={`rounded-full border px-2.5 py-0.5 text-xs font-medium transition ${style}`}>
              {o.name}{sel && pos === 0 ? firstBadge : ""}
            </button>
          );
        })}
        {stale.map((n) => (
          <button key={n} type="button" title={staleNote} disabled={disabled}
            onClick={() => onChange(selected.filter((x) => x !== n))}
            className="rounded-full border border-red-300 bg-red-50 px-2.5 py-0.5 text-xs font-medium text-red-700 line-through hover:bg-red-100 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300 dark:hover:bg-red-950">
            {n} ✕
          </button>
        ))}
        {disabled && disabledNote && (
          <span className="text-xs text-neutral-400">{disabledNote}</span>
        )}
      </div>
    </Field>
  );
}
