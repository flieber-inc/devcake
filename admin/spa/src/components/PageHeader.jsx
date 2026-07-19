import React from "react";

export default function PageHeader({ title, subtitle, actions }) {
  return (
    <div className="mb-4 flex flex-wrap items-center justify-between gap-3 sm:mb-6">
      <div className="min-w-0 flex-1">
        <h1 className="font-display text-xl font-extrabold tracking-tight">{title}</h1>
        {subtitle && (
          <p className="mt-0.5 text-sm text-neutral-500 dark:text-neutral-400">{subtitle}</p>
        )}
      </div>
      {actions && <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto sm:justify-end">{actions}</div>}
    </div>
  );
}
