import React from "react";
import { Help } from "./Field.jsx";

// Cursor-style settings row: label + one-line description on the left, the
// control on the right; long explanations live in the Help popover. Stack
// rows inside a `divide-y` container. The heading is a <label> only when
// htmlFor is given — a bare label around non-inputs forwards clicks.
export default function SettingRow({ label, desc, help, htmlFor, children }) {
  const Heading = htmlFor ? "label" : "span";
  return (
    <div className="flex flex-col gap-2 py-3 first:pt-1 last:pb-1 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
      <div className="min-w-0 sm:max-w-[34rem]">
        <Heading {...(htmlFor ? { htmlFor } : {})} className="text-sm font-medium">
          {label}
          {help && <Help text={help} />}
        </Heading>
        {desc && (
          <p className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">{desc}</p>
        )}
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-3">{children}</div>
    </div>
  );
}
