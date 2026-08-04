import React from "react";
import { ExternalLink } from "lucide-react";
import StageGlyph from "./StageGlyph.jsx";
import { buildDepIndex, chainAround, inCycle, nodeId, nodeStatus, parseCycles } from "../lib/deps.js";

// Read-only dependency chain for the mission drawer — blockers above, this
// mission highlighted, dependents below. Strictly a RENDER of the live poll
// snapshot: DevCake never draws or deletes an edge (ADR-0007/0012 — the
// port has no remove_relation), so the only affordances here are navigation
// (click a resolved node to open its drawer) and the PMO escape link.
// Div/border construction only — no SVG, no canvas (DESIGN.md §1: the
// StageGlyph is the one flourish), and it owes rendering at any width.

const STATUS_TONE = {
  satisfied: "text-neutral-400 dark:text-neutral-500",
  guard: "text-amber-700 dark:text-amber-300",
  open: "text-neutral-500 dark:text-neutral-400",
};

function Node({ row, status, cycle, multiPmo, onOpen }) {
  const inner = (
    <>
      <span className="flex w-3.5 shrink-0 justify-center">
        {row.mission_type && <StageGlyph stage={row.mission_type} />}
      </span>
      <span className="shrink-0 truncate font-mono text-[11px] text-neutral-500 dark:text-neutral-400">
        {multiPmo && (
          <span className="text-neutral-400 dark:text-neutral-500">{row.instance}·</span>
        )}
        {row.key}
      </span>
      <span
        className="min-w-0 flex-1 truncate text-xs text-neutral-700 dark:text-neutral-300"
        title={row.title}
      >
        {row.title}
      </span>
      <span
        // shrink+truncate, not shrink-0: a long guard status must ellipsize
        // (full text in the tooltip), never clip mid-word or force width
        className={`min-w-0 shrink truncate text-right text-[11px] ${cycle ? STATUS_TONE.guard : STATUS_TONE[status.tone]}`}
        title={cycle ? "in a dependency cycle" : status.text}
      >
        {cycle ? "in a dependency cycle" : status.text}
      </span>
    </>
  );
  if (!onOpen) {
    return <div className="flex items-center gap-2 px-2 py-1">{inner}</div>;
  }
  return (
    <button
      type="button"
      onClick={onOpen}
      className="flex w-full items-center gap-2 rounded px-2 py-1 text-left transition hover:bg-stone-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:hover:bg-neutral-900"
    >
      {inner}
    </button>
  );
}

function UnresolvedNode({ id }) {
  return (
    <div
      className="flex items-center gap-2 px-2 py-1"
      title="This id no longer appears in any PMO poll snapshot — usually a mission that finished and aged out. Your PMO has the full story."
    >
      <span className="w-3.5 shrink-0" />
      <span className="shrink-0 truncate font-mono text-[11px] text-neutral-400 dark:text-neutral-500">
        {id}
      </span>
      <span className="min-w-0 flex-1" />
      <span className="min-w-0 shrink truncate text-[11px] text-neutral-400 dark:text-neutral-500">
        no longer in any snapshot
      </span>
    </div>
  );
}

export default function DependencyPanel({ mission, rows, adoptionMode, cycles, multiPmo, onOpenMission }) {
  // the drawer captures its row at click time; the chain must read the LIVE
  // row so edges and statuses track the 10s board poll
  const live = (rows || []).find((r) => nodeId(r) === nodeId(mission)) || mission;

  if (live.kind === "project") {
    return (
      <section aria-label="Dependencies" data-testid="dep-panel" className="border-b border-neutral-100 pb-3 dark:border-neutral-800">
        <p className="text-xs text-neutral-500 dark:text-neutral-400">
          Projects carry no dependency relations — dependencies live on their issues.
        </p>
      </section>
    );
  }

  const index = buildDepIndex(rows || []);
  const { blockers, dependents, upstream, downstream } = chainAround(index, live);
  if (blockers.length === 0 && dependents.length === 0) return null;

  const cycleMembers = parseCycles(cycles);
  const chainHasCycle =
    inCycle(live, cycleMembers) ||
    blockers.some((b) => b.row && inCycle(b.row, cycleMembers)) ||
    dependents.some((d) => inCycle(d, cycleMembers));
  const beyond = upstream.length + downstream.length;

  return (
    <section
      aria-label="Dependencies"
      data-testid="dep-panel"
      className="border-b border-neutral-100 pb-3 dark:border-neutral-800"
    >
      <div className="mb-1.5 flex items-baseline gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
          Dependencies
        </h3>
        <span className="min-w-0 flex-1 truncate text-[11px] text-neutral-400 dark:text-neutral-500">
          Read from your PMO on every poll — DevCake never adds or removes relations.
        </span>
        {live.url && (
          <a
            href={live.url}
            target="_blank"
            rel="noopener"
            className="inline-flex shrink-0 items-center gap-1 text-[11px] text-accent-700 underline underline-offset-2 dark:text-accent-300"
          >
            Manage in PMO <ExternalLink size={10} aria-hidden />
          </a>
        )}
      </div>
      {chainHasCycle && (
        <p className="mb-1.5 rounded border border-amber-300 bg-amber-50 px-2 py-1 text-[11px] text-amber-800 dark:border-amber-800 dark:bg-amber-950/60 dark:text-amber-300">
          This chain contains a dependency cycle — its members will never start
          until a relation is deleted in your PMO.
        </p>
      )}
      <div className="ml-1.5 border-l-2 border-neutral-200 pl-2 dark:border-neutral-800">
        {blockers.length > 0 && (
          <div data-testid="dep-blockers">
            <p className="px-2 pt-0.5 text-[10px] font-medium uppercase tracking-wide text-neutral-400 dark:text-neutral-500">
              Blocked by ({blockers.length})
            </p>
            {blockers.map(({ key, row }) =>
              row ? (
                <Node
                  key={nodeId(row)}
                  row={row}
                  status={nodeStatus(row, adoptionMode, { asBlocker: true })}
                  cycle={inCycle(row, cycleMembers)}
                  multiPmo={multiPmo}
                  onOpen={() => onOpenMission(row)}
                />
              ) : (
                <UnresolvedNode key={key} id={key} />
              ),
            )}
          </div>
        )}
        <div
          data-testid="dep-current"
          className="-ml-[10px] my-0.5 flex items-center gap-2 rounded-r border-l-2 border-accent-600 bg-accent-50/60 py-1 pl-2 pr-2 dark:border-accent-400 dark:bg-accent-950/40"
        >
          <span className="flex w-3.5 shrink-0 justify-center">
            {live.mission_type && <StageGlyph stage={live.mission_type} />}
          </span>
          <span className="shrink-0 font-mono text-[11px] text-neutral-700 dark:text-neutral-200">
            {multiPmo && (
              <span className="text-neutral-400 dark:text-neutral-500">{live.instance}·</span>
            )}
            {live.key}
          </span>
          <span className="min-w-0 flex-1 truncate text-xs font-medium text-neutral-900 dark:text-neutral-100">
            {live.title}
          </span>
          <span className="shrink-0 text-[10px] font-medium uppercase tracking-wide text-accent-700 dark:text-accent-300">
            this mission
          </span>
        </div>
        {dependents.length > 0 && (
          <div data-testid="dep-dependents">
            <p className="px-2 pt-0.5 text-[10px] font-medium uppercase tracking-wide text-neutral-400 dark:text-neutral-500">
              Blocks ({dependents.length})
            </p>
            {dependents.map((row) => (
              <Node
                key={nodeId(row)}
                row={row}
                status={nodeStatus(row, adoptionMode)}
                cycle={inCycle(row, cycleMembers)}
                multiPmo={multiPmo}
                onOpen={() => onOpenMission(row)}
              />
            ))}
          </div>
        )}
      </div>
      {beyond > 0 && (
        <details className="mt-1.5">
          <summary className="cursor-pointer text-[11px] text-accent-700 underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:text-accent-300">
            Full chain ({beyond} more)
          </summary>
          <div className="ml-1.5 mt-1 border-l-2 border-neutral-200 pl-2 dark:border-neutral-800">
            {upstream.length > 0 && (
              <p className="px-2 pt-0.5 text-[10px] font-medium uppercase tracking-wide text-neutral-400 dark:text-neutral-500">
                Further upstream
              </p>
            )}
            {upstream.map((row) => (
              <Node
                key={nodeId(row)}
                row={row}
                status={nodeStatus(row, adoptionMode, { asBlocker: true })}
                cycle={inCycle(row, cycleMembers)}
                multiPmo={multiPmo}
                onOpen={() => onOpenMission(row)}
              />
            ))}
            {downstream.length > 0 && (
              <p className="px-2 pt-0.5 text-[10px] font-medium uppercase tracking-wide text-neutral-400 dark:text-neutral-500">
                Further downstream
              </p>
            )}
            {downstream.map((row) => (
              <Node
                key={nodeId(row)}
                row={row}
                status={nodeStatus(row, adoptionMode)}
                cycle={inCycle(row, cycleMembers)}
                multiPmo={multiPmo}
                onOpen={() => onOpenMission(row)}
              />
            ))}
          </div>
        </details>
      )}
    </section>
  );
}
