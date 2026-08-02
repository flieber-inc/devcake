import React from "react";
import { Help } from "./Field.jsx";

export function Card({ className = "", children, ...props }) {
  return (
    <div
      {...props}
      className={`rounded-card border border-neutral-200 bg-surface-raised shadow-card dark:border-neutral-800 dark:bg-surface-raised-dark ${className}`}
    >
      {children}
    </div>
  );
}

export function Section({ id, title, description, help, actions, children }) {
  return (
    <Card id={id} className="scroll-mt-6 p-5 sm:p-6">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        {/* min-w-0 flex-1: a long description must wrap INSIDE this div —
            without it the title div claims the full row and flex-wrap drops
            `actions` to a second row at the LEFT, where a right-anchored
            MoreMenu popover extends over the sidebar and is clipped by the
            main scroll container (found via tests/repos.mjs) */}
        <div className="min-w-0 flex-1">
          <h3 className="text-base font-semibold tracking-tight">
            {title}
            {help && <Help text={help} />}
          </h3>
          {description && (
            <p className="mt-0.5 text-sm text-neutral-500 dark:text-neutral-400">{description}</p>
          )}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
      <div className="space-y-4">{children}</div>
    </Card>
  );
}
