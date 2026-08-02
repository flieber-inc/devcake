import React from "react";

// Mobile/collapsed-rail switcher between the two Adapters pages — the exact
// counterpart of ConfigPage's section chip row: the sidebar's indented
// sub-entries exist only in the expanded drawer, so narrow viewports (where
// the sidebar is an icon rail and the Adapters icon lands on Repositories)
// need an on-page path to the sibling page.
const TABS = [
  { page: "repos", href: "#/repos", label: "Repositories" },
  { page: "pmo", href: "#/pmo", label: "PMO" },
];

export default function AdapterTabs({ page }) {
  return (
    <div className="sticky top-0 z-20 -mx-4 flex gap-1.5 overflow-x-auto bg-surface/90 px-4 py-2 backdrop-blur dark:bg-surface-dark/90 lg:hidden">
      {TABS.map((t) => (
        <a key={t.page} href={t.href}
          className={`shrink-0 rounded-full border px-3 py-1 text-xs font-medium ${
            page === t.page
              ? "border-accent-300 bg-accent-50 font-semibold text-accent-800 dark:border-accent-800 dark:bg-accent-950/60 dark:text-accent-200"
              : "border-neutral-200 bg-surface-raised text-neutral-600 dark:border-neutral-800 dark:bg-surface-raised-dark dark:text-neutral-300"
          }`}>
          {t.label}
        </a>
      ))}
    </div>
  );
}
