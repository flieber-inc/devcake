import React from "react";
import { ExternalLink } from "lucide-react";
import MoreMenu from "./MoreMenu.jsx";
import StageGlyph from "./StageGlyph.jsx";
import { relTime } from "../lib/format.js";
import { contextActions, needsHumanReason } from "../lib/board.js";

// One mission on the board. A full-card button keeps the generous pointer
// target without turning links and menus into invalid nested controls.
export default function MissionCard({ row, syncing, onOpen, onAction }) {
  const stage = row.mission_type;
  const badge = needsHumanReason(row.labels);
  const items = contextActions(row);
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
    <article aria-label={`${row.key}: ${row.title}`} className="group relative rounded-card border border-neutral-200 bg-surface-raised p-3 shadow-card transition hover:border-accent-400 focus-within:border-accent-400 focus-within:ring-2 focus-within:ring-accent-500/60 dark:border-neutral-800 dark:bg-surface-raised-dark xl:p-2 2xl:p-3">
      <button
        type="button"
        aria-label={`Open mission ${row.key}: ${row.title}`}
        onClick={onOpen}
        className="absolute inset-0 z-0 cursor-pointer rounded-card focus:outline-none"
      />
      <div className="pointer-events-none relative z-10">
        <div className="mb-1.5 flex min-w-0 items-center gap-2 xl:gap-1 2xl:gap-2">
          {stage && <StageGlyph stage={stage} />}
          <span className="min-w-0 truncate font-mono text-xs text-neutral-500 dark:text-neutral-400">
            {row.key}
          </span>
          <span className="grow" />
          {syncing && (
            <span
              title="Waiting for the next poll cycle to confirm this change"
              className="shrink-0 rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-medium text-blue-800 dark:bg-blue-950 dark:text-blue-300 xl:px-1.5"
            >
              syncing…
            </span>
          )}
          {menuItems.length > 0 && (
            <span className="pointer-events-auto shrink-0">
              <MoreMenu label={`More actions for ${row.key}`} items={menuItems} />
            </span>
          )}
        </div>
        <p
          className="mb-1.5 line-clamp-2 text-sm font-medium text-neutral-900 dark:text-neutral-100 xl:text-xs 2xl:text-sm"
          title={row.title}
        >
          {row.title}
        </p>
        {badge && (
          <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-300 xl:text-[10px] 2xl:text-[11px]">
            {badge}
          </p>
        )}
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-neutral-500 dark:text-neutral-400">
          {row.priority && (
            <span className="rounded bg-stone-100 px-1.5 py-0.5 font-medium uppercase tracking-wide dark:bg-neutral-800 xl:px-1 xl:text-[10px] 2xl:px-1.5 2xl:text-[11px]">
              {row.priority}
            </span>
          )}
          {row.repo && (
            <span className="min-w-0 truncate xl:hidden 2xl:inline" title={`repo: ${row.repo}`}>
              {row.repo}
            </span>
          )}
          {row.url && (
            <a
              href={row.url}
              target="_blank"
              rel="noopener"
              title="Open in Linear"
              className="pointer-events-auto inline-flex items-center gap-0.5 text-accent-700 underline underline-offset-2 dark:text-accent-300 xl:hidden 2xl:inline-flex"
            >
              open <ExternalLink size={9} aria-hidden />
            </a>
          )}
          {row.updated_at && (
            <span className="ml-auto shrink-0" title={row.updated_at}>
              {relTime(row.updated_at)}
            </span>
          )}
        </div>
        {row.reason && !row.schedulable && (
          <p
            className="mt-1.5 line-clamp-2 text-[11px] text-neutral-500 dark:text-neutral-400 xl:hidden 2xl:block"
            title={row.reason}
          >
            {row.reason}
          </p>
        )}
      </div>
    </article>
  );
}
