import React from "react";
import { ExternalLink } from "lucide-react";
import MoreMenu from "./MoreMenu.jsx";
import StageGlyph from "./StageGlyph.jsx";
import { relTime } from "../lib/format.js";
import { contextActions, needsHumanReason } from "../lib/board.js";

// Priority is a quiet dot, not a chip: on a real fleet nearly every mission
// is "medium", so a labelled chip repeated down the list carries no scan
// value — color only speaks when a row deviates. Tooltip spells it out.
const PRIORITY_DOT = {
  urgent: "bg-red-500",
  high: "bg-amber-500",
  medium: "bg-neutral-300 dark:bg-neutral-600",
  low: "bg-neutral-200 dark:bg-neutral-800",
};

// One mission in a stage section — a single dense line (board re-decision
// 2026-08-02, admin/spa/DESIGN.md §2: the kanban card's five stacked rows
// collapse to one so the title gets the page's width). Click anywhere opens
// the drawer; the ⋯ menu and the ↗ link stop propagation.
export default function MissionRow({ row, multiPmo, syncing, sectionReason, onOpen, onAction }) {
  const badge = needsHumanReason(row.labels);
  const items = contextActions(row);
  // Park is destructive-ish (stops scheduling) — surface it as a `danger`
  // menu item so it visually separates from Retry/Resume/Unpark.
  const menuItems = [
    ...items.map((it) => ({
      label: it.label,
      danger: it.id === "park",
      desc: it.id === "park"
        ? "Stops DevCake from scheduling new work on this mission."
        : undefined,
      onClick: () => onAction(it.id),
    })),
    row.url && {
      label: "Open in PMO",
      external: true,
      onClick: () => window.open(row.url, "_blank", "noopener"),
    },
  ].filter(Boolean);
  // The section header carries the reason every row in it shares; a row
  // spells its own out only when it deviates.
  const reason = row.reason && !row.schedulable && row.reason !== sectionReason
    ? row.reason : null;
  const dot = PRIORITY_DOT[(row.priority || "").toLowerCase()];

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onOpen()}
      className="flex cursor-pointer items-center gap-2.5 border-t border-neutral-100 px-3 py-2 transition first:border-t-0 hover:bg-stone-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:border-neutral-800 dark:hover:bg-neutral-900"
    >
      {/* slot is reserved even without a glyph so keys align down the list */}
      <span className="flex w-3.5 shrink-0 justify-center">
        {row.mission_type && <StageGlyph stage={row.mission_type} />}
      </span>
      {/* PMO provenance rides the machine identifier as a dimmed mono
          prefix (branch/run-id convention, ADR-0009) — only when there is
          more than one PMO to tell apart; a chip would repeat fleet-wide
          with no scan value (the priority-dot argument, DESIGN.md §2). */}
      <span
        className={`${multiPmo ? "w-40" : "w-32"} shrink-0 truncate font-mono text-xs text-neutral-500 dark:text-neutral-400`}
        title={multiPmo ? `${row.instance} · ${row.key}` : row.key}
      >
        {multiPmo && (
          <span className="text-neutral-400 dark:text-neutral-500">{row.instance}·</span>
        )}
        {row.key}
      </span>
      <span
        className="min-w-0 flex-1 truncate text-sm font-medium text-neutral-900 dark:text-neutral-100"
        title={row.title}
      >
        {row.title}
      </span>
      {badge && (
        <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-300">
          {badge}
        </span>
      )}
      {syncing && (
        <span
          title="Waiting for the next poll cycle to confirm this change"
          className="shrink-0 rounded-full bg-accent-100 px-2 py-0.5 text-[10px] font-medium text-accent-800 dark:bg-accent-950 dark:text-accent-300"
        >
          syncing…
        </span>
      )}
      {reason && (
        <span
          className="hidden max-w-[14rem] truncate text-[11px] text-neutral-500 lg:inline dark:text-neutral-400"
          title={row.reason}
        >
          {reason}
        </span>
      )}
      {row.repo && (
        <span
          className="hidden max-w-[10rem] shrink-0 truncate text-[11px] text-neutral-500 md:inline dark:text-neutral-400"
          title={`repo: ${row.repo}`}
        >
          {row.repo}
        </span>
      )}
      {dot && (
        <span
          role="img"
          aria-label={`priority: ${row.priority}`}
          title={`priority: ${row.priority}`}
          className={`h-2 w-2 shrink-0 rounded-full ${dot}`}
        />
      )}
      {row.updated_at && (
        <span
          className="hidden w-12 shrink-0 text-right text-[11px] tabular-nums text-neutral-500 sm:inline dark:text-neutral-400"
          title={row.updated_at}
        >
          {relTime(row.updated_at)}
        </span>
      )}
      {row.url && (
        <a
          href={row.url}
          target="_blank"
          rel="noopener"
          onClick={(e) => e.stopPropagation()}
          title="Open in PMO"
          aria-label={`Open ${row.key} in PMO`}
          className="shrink-0 rounded p-1 text-accent-700 hover:bg-stone-100 dark:text-accent-300 dark:hover:bg-neutral-800"
        >
          <ExternalLink size={13} aria-hidden />
        </a>
      )}
      <span onClick={(e) => e.stopPropagation()}>
        {menuItems.length > 0 && (
          <MoreMenu label={`More actions for ${row.key}`} items={menuItems} />
        )}
      </span>
    </div>
  );
}
