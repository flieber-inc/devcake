import React from "react";
import { ExternalLink } from "lucide-react";
import MoreMenu from "./MoreMenu.jsx";
import StageGlyph from "./StageGlyph.jsx";
import { relTime } from "../lib/format.js";
import { contextActions, needsHumanReason } from "../lib/board.js";

// One mission on the board. Click anywhere on the card opens the drawer;
// MoreMenu clicks stop propagation so the ⋯ button doesn't also open it.
export default function MissionCard({ row, syncing, onOpen, onAction }) {
  const stage = row.mission_type;
  const badge = needsHumanReason(row.labels);
  const items = contextActions(row);
  // Park is destructive-ish (stops scheduling) — surface it as a `danger` menu
  // item so it visually separates from Retry/Resume/Unpark.
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
      label: "Open in Linear",
      external: true,
      onClick: () => window.open(row.url, "_blank", "noopener"),
    },
  ].filter(Boolean);

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onOpen()}
      className="cursor-pointer rounded-card border border-neutral-200 bg-surface-raised p-3 shadow-card transition hover:border-accent-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:border-neutral-800 dark:bg-surface-raised-dark"
    >
      <div className="mb-1.5 flex items-center gap-2">
        {stage && <StageGlyph stage={stage} />}
        <span className="font-mono text-xs text-neutral-500 dark:text-neutral-400">
          {row.key}
        </span>
        <span className="grow" />
        {syncing && (
          <span
            title="Waiting for the next poll cycle to confirm this change"
            className="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-medium text-blue-800 dark:bg-blue-950 dark:text-blue-300"
          >
            syncing…
          </span>
        )}
        <span onClick={(e) => e.stopPropagation()}>
          {menuItems.length > 0 && (
            <MoreMenu label={`More actions for ${row.key}`} items={menuItems} />
          )}
        </span>
      </div>
      <p
        className="mb-1.5 line-clamp-2 text-sm font-medium text-neutral-900 dark:text-neutral-100"
        title={row.title}
      >
        {row.title}
      </p>
      {badge && (
        <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-300">
          {badge}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-neutral-500 dark:text-neutral-400">
        {row.priority && (
          <span className="rounded bg-stone-100 px-1.5 py-0.5 font-medium uppercase tracking-wide dark:bg-neutral-800">
            {row.priority}
          </span>
        )}
        {row.repo && (
          <span className="truncate" title={`repo: ${row.repo}`}>
            {row.repo}
          </span>
        )}
        {row.url && (
          <a
            href={row.url}
            target="_blank"
            rel="noopener"
            onClick={(e) => e.stopPropagation()}
            title="Open in Linear"
            className="inline-flex items-center gap-0.5 text-accent-700 underline underline-offset-2 dark:text-accent-300"
          >
            open <ExternalLink size={9} aria-hidden />
          </a>
        )}
        {row.updated_at && (
          <span className="ml-auto" title={row.updated_at}>
            {relTime(row.updated_at)}
          </span>
        )}
      </div>
      {row.reason && !row.schedulable && (
        <p
          className="mt-1.5 line-clamp-2 text-[11px] text-neutral-500 dark:text-neutral-400"
          title={row.reason}
        >
          {row.reason}
        </p>
      )}
    </div>
  );
}
