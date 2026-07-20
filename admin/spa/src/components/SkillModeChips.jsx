import React from "react";
import { Field } from "./Field.jsx";

// Tri-state skill selector for Dev Types (docs/11):
//   off → available (installed, consult-optional) → required (soft-force
//   prompt) → off. Catalog order is written back so uncheck/recheck does
//   not create a reorder-only dirty diff. Stale / catalog-unavailable
//   behavior mirrors SelectionChips.

const MODE = { off: 0, available: 1, required: 2 };

function modeOf(name, available, required) {
  if ((required || []).includes(name)) return MODE.required;
  if ((available || []).includes(name)) return MODE.available;
  return MODE.off;
}

function cycle(m) {
  if (m === MODE.off) return MODE.available;
  if (m === MODE.available) return MODE.required;
  return MODE.off;
}

function chipClass(mode, blocked) {
  if (blocked && mode === MODE.off) {
    return "cursor-not-allowed border-neutral-200 text-neutral-300 dark:border-neutral-800 dark:text-neutral-600";
  }
  if (mode === MODE.required) {
    return `border-accent-600 bg-accent-100 font-semibold text-accent-900 dark:border-accent-500 dark:bg-accent-900/80 dark:text-accent-100${
      blocked ? " cursor-not-allowed opacity-60" : ""}`;
  }
  if (mode === MODE.available) {
    return `border-accent-400 bg-accent-50 text-accent-800 dark:border-accent-700 dark:bg-accent-950/70 dark:text-accent-200${
      blocked ? " cursor-not-allowed opacity-60" : ""}`;
  }
  return "border-neutral-300 text-neutral-500 hover:bg-stone-100 dark:border-neutral-700 dark:hover:bg-neutral-800";
}

function titleFor(mode, optionTitle, disabledNote) {
  if (disabledNote) return disabledNote;
  const base = optionTitle || "";
  if (mode === MODE.required) {
    return `${base}${base ? " — " : ""}Required: installed and soft-forced in the prompt (instructed to consult; not kernel-enforced). Click to clear.`;
  }
  if (mode === MODE.available) {
    return `${base}${base ? " — " : ""}Available: installed; the Dev may consult via description match. Click again to require.`;
  }
  return `${base}${base ? " — " : ""}Off. Click to install as Available.`;
}

/** Normalize available + required lists to catalog order; drop required not in available. */
export function normalizeSkillSelection(available, required, catalogNames) {
  const availSet = new Set(available || []);
  const reqSet = new Set((required || []).filter((n) => availSet.has(n)));
  const known = catalogNames || [];
  const skills = [
    ...known.filter((n) => availSet.has(n)),
    ...[...availSet].filter((n) => !known.includes(n)),
  ];
  const skills_required = [
    ...known.filter((n) => reqSet.has(n)),
    ...[...reqSet].filter((n) => !known.includes(n)),
  ];
  return { skills, skills_required };
}

export default function SkillModeChips({
  label = "Skills",
  help,
  options = [],                 // [{ name, title? }]
  available = [],
  required = [],
  onChange,                     // ({ skills, skills_required }) => void
  emptyNote = "nothing to select yet",
  staleNote = "not in the skill store — skipped at dispatch; click to remove",
  disabled = false,
  disabledNote = "",
  optionsUnavailable = false,
  unavailableNote = "catalog unavailable — selection preserved until it loads",
}) {
  const catalog = options.map((o) => o.name);
  const stale = (available || []).filter((n) => !options.some((o) => o.name === n));

  const apply = (nextAvail, nextReq) => {
    onChange(normalizeSkillSelection(nextAvail, nextReq, catalog));
  };

  const onChip = (name) => {
    if (disabled) return;
    const m = cycle(modeOf(name, available, required));
    let nextAvail = (available || []).filter((n) => n !== name);
    let nextReq = (required || []).filter((n) => n !== name);
    if (m === MODE.available || m === MODE.required) nextAvail = [...nextAvail, name];
    if (m === MODE.required) nextReq = [...nextReq, name];
    apply(nextAvail, nextReq);
  };

  const removeStale = (name) => {
    apply(
      (available || []).filter((n) => n !== name),
      (required || []).filter((n) => n !== name),
    );
  };

  return (
    <Field label={label} help={help}>
      <div className="flex flex-wrap items-center gap-1.5 pt-1">
        {options.length === 0 && stale.length === 0 && (
          <span className="text-xs text-neutral-500 dark:text-neutral-400">{emptyNote}</span>
        )}
        {options.map((o) => {
          const mode = modeOf(o.name, available, required);
          const blocked = disabled;
          return (
            <button key={o.name} type="button" disabled={blocked}
              title={titleFor(mode, o.title, disabled ? disabledNote : "")}
              onClick={() => onChip(o.name)}
              className={`rounded-full border px-2.5 py-0.5 text-xs font-medium transition ${chipClass(mode, blocked)}`}>
              {o.name}{mode === MODE.required ? " · req" : ""}
            </button>
          );
        })}
        {stale.map((n) => (
          optionsUnavailable ? (
            <span key={n} title={unavailableNote}
              className={`rounded-full border px-2.5 py-0.5 text-xs font-medium opacity-70 ${
                modeOf(n, available, required) === MODE.required
                  ? "border-accent-600 bg-accent-100 text-accent-900 dark:border-accent-500 dark:bg-accent-900/80 dark:text-accent-100"
                  : "border-accent-400 bg-accent-50 text-accent-800 dark:border-accent-700 dark:bg-accent-950/70 dark:text-accent-200"
              }`}>
              {n}{modeOf(n, available, required) === MODE.required ? " · req" : ""}
            </span>
          ) : (
            <button key={n} type="button" title={staleNote} disabled={disabled}
              onClick={() => removeStale(n)}
              className="rounded-full border border-red-300 bg-red-50 px-2.5 py-0.5 text-xs font-medium text-red-700 line-through hover:bg-red-100 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300 dark:hover:bg-red-950">
              {n} ✕
            </button>
          )
        ))}
        {disabled && disabledNote && (
          <span className="text-xs text-neutral-500 dark:text-neutral-400">{disabledNote}</span>
        )}
      </div>
      {(options.length > 0 || stale.length > 0) && !disabled && (
        <p className="mt-1.5 text-xs text-neutral-500 dark:text-neutral-400">
          Click to cycle: off → Available (may consult) → Required (prompt soft-force) → off.
          Required is instructional, not kernel-enforced.
        </p>
      )}
    </Field>
  );
}
