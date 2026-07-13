import React from "react";

// tri-state service dot: true = healthy, false = down, undefined = unknown
export default function StatusDot({ ok, label }) {
  const color = ok === true ? "bg-green-500" : ok === false ? "bg-red-500" : "bg-neutral-400";
  return (
    <span className="flex items-center gap-1.5 text-xs text-neutral-500 dark:text-neutral-400">
      <span className={`inline-block h-2 w-2 rounded-full ${color}`} />
      {label}
    </span>
  );
}
