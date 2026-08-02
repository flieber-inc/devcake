import React, { useState } from "react";
import { Pin } from "lucide-react";
import { Field, Input } from "./Field.jsx";

// The panel's standard multi-select (docs/11 "Multi-select convention"):
// ordered toggle chips over a catalog of names. Selection order = click
// order — callers for whom order is meaningless should normalize it in
// onChange (the draft diff compares arrays order-sensitively).
//
// 2026-08-02 (bulk-scale): SELECTED chips render first, in selection order —
// the order is load-bearing where `firstBadge` is set (first work repo =
// default routing target) and was previously invisible (chips rendered in
// catalog order). Non-first selected chips get a pin "make default"
// affordance when firstBadge is set. Catalogs past `searchThreshold`
// options gain a search box that narrows the catalog chips (capped at
// `matchCap` matches) — search narrows the chip catalog, it never replaces
// the chip control.
//
// A selected name whose option no longer exists renders as a red
// strikethrough ✕ chip: visible AND removable, so a stale entry can never
// wedge the Save PUT. Every new multi-select field uses this component —
// not checkbox lists, not multi-select dropdowns.
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
  // when true (e.g. the catalog fetch failed), a selected name absent from
  // options is NOT assumed stale — it renders as a neutral present chip, not a
  // red click-to-remove one, so a transient outage can't read as "deleted"
  // and invite accidental removal (re-audit #7)
  optionsUnavailable = false,
  unavailableNote = "catalog unavailable — selection preserved until it loads",
  searchThreshold = 12,
  matchCap = 30,
}) {
  const [query, setQuery] = useState("");
  const byName = new Map(options.map((o) => [o.name, o]));
  const stale = selected.filter((n) => !byName.has(n));
  const chosen = selected.filter((n) => byName.has(n));
  const searching = options.length > searchThreshold;
  const q = query.trim().toLowerCase();
  const catalog = options
    .filter((o) => !selected.includes(o.name))
    .filter((o) => !q || o.name.toLowerCase().includes(q));
  const shown = searching ? catalog.slice(0, matchCap) : catalog;
  const hiddenCount = catalog.length - shown.length;

  return (
    <Field label={label} help={help}>
      <div className="space-y-1.5 pt-1">
        {/* selected chips, in SELECTION order — position 1 is the default */}
        {(chosen.length > 0 || stale.length > 0) && (
          <div className="flex flex-wrap items-center gap-1.5">
            {chosen.map((n, i) => (
              <span key={n} className="inline-flex items-center gap-0.5">
                <button type="button" disabled={disabled}
                  title={byName.get(n)?.title}
                  onClick={() => onChange(selected.filter((x) => x !== n))}
                  className={`rounded-full border px-2.5 py-0.5 text-xs font-medium transition border-accent-400 bg-accent-50 text-accent-800 dark:border-accent-700 dark:bg-accent-950/70 dark:text-accent-200${
                    disabled ? " cursor-not-allowed opacity-60" : ""}`}>
                  {n}{i === 0 ? firstBadge : ""}
                </button>
                {firstBadge && i > 0 && !disabled && (
                  <button type="button"
                    aria-label={`Make ${n} the default`}
                    title="Make default — moves it to the front; missions without a repo marker route to the default"
                    onClick={() => onChange([n, ...selected.filter((x) => x !== n)])}
                    className="rounded p-0.5 text-neutral-400 transition hover:text-accent-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:text-neutral-500 dark:hover:text-accent-300">
                    <Pin size={11} aria-hidden />
                  </button>
                )}
              </span>
            ))}
            {stale.map((n) => (
              optionsUnavailable ? (
                // catalog couldn't load — show as a present chip, and DISABLE it
                // (re-audit #31 #3): while the catalog is unknown there is no
                // option chip to re-select from, so a misclick-remove would be
                // unrecoverable in-UI. Non-interactive preserves the selection
                // until the catalog loads. Title must NOT say "click to remove".
                <span key={n} title={unavailableNote}
                  className="rounded-full border px-2.5 py-0.5 text-xs font-medium border-accent-400 bg-accent-50 text-accent-800 opacity-70 dark:border-accent-700 dark:bg-accent-950/70 dark:text-accent-200">
                  {n}
                </span>
              ) : (
                <button key={n} type="button" title={staleNote} disabled={disabled}
                  onClick={() => onChange(selected.filter((x) => x !== n))}
                  className="rounded-full border border-red-300 bg-red-50 px-2.5 py-0.5 text-xs font-medium text-red-700 line-through hover:bg-red-100 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300 dark:hover:bg-red-950">
                  {n} ✕
                </button>
              )
            ))}
          </div>
        )}
        {/* catalog search — only past the threshold, so small catalogs keep
            today's zero-friction chip cloud */}
        {searching && !disabled && (
          <span className="relative block w-64">
            <Input className="pr-7" value={query}
              placeholder={`Search ${options.length} entries…`}
              aria-label={`Search ${label || "options"}`}
              onChange={(e) => setQuery(e.target.value)} />
            {query && (
              <button type="button" aria-label="Clear search"
                onClick={() => setQuery("")}
                className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-neutral-500 hover:text-neutral-800 dark:text-neutral-400 dark:hover:text-neutral-100">
                ✕
              </button>
            )}
          </span>
        )}
        <div className="flex flex-wrap items-center gap-1.5">
          {options.length === 0 && stale.length === 0 && (
            <span className="text-xs text-neutral-500 dark:text-neutral-400">{emptyNote}</span>
          )}
          {shown.map((o) => {
            const blocked = disabled || o.disabled;
            return (
              <button key={o.name} type="button" disabled={blocked}
                title={disabled ? undefined : o.disabled ? o.disabledNote : o.title}
                onClick={() => onChange([...selected, o.name])}
                className={`rounded-full border px-2.5 py-0.5 text-xs font-medium transition ${
                  blocked
                    ? "cursor-not-allowed border-neutral-200 text-neutral-300 dark:border-neutral-800 dark:text-neutral-600"
                    : "border-neutral-300 text-neutral-500 hover:bg-stone-100 dark:border-neutral-700 dark:hover:bg-neutral-800"}`}>
                {o.name}
              </button>
            );
          })}
          {hiddenCount > 0 && (
            <span className="text-xs text-neutral-500 dark:text-neutral-400">
              {hiddenCount} more — refine the search
            </span>
          )}
          {searching && q && catalog.length === 0 && (
            <span className="text-xs text-neutral-500 dark:text-neutral-400">
              no matches for “{query.trim()}”
            </span>
          )}
        </div>
        {disabled && disabledNote && (
          <span className="text-xs text-neutral-500 dark:text-neutral-400">{disabledNote}</span>
        )}
      </div>
    </Field>
  );
}
