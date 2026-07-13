import React from "react";

const KINDS = {
  primary: "bg-accent text-white hover:bg-accent-700",
  ghost:
    "border border-neutral-300 hover:bg-neutral-50 dark:border-neutral-700 dark:hover:bg-neutral-800",
  danger: "bg-red-700 text-white hover:bg-red-800",
  "danger-ghost":
    "border border-red-300 text-red-700 hover:bg-red-50 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-950",
};

const SIZES = { sm: "px-2 py-1 text-xs", md: "px-3 py-1.5 text-sm" };

export default function Button({
  children, kind = "primary", size = "md", icon: Icon, className = "", ...props
}) {
  return (
    <button
      {...props}
      className={`inline-flex items-center justify-center gap-1.5 rounded-md font-medium transition
        focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60
        disabled:opacity-40 ${SIZES[size]} ${KINDS[kind]} ${className}`}
    >
      {Icon && <Icon size={size === "sm" ? 13 : 15} strokeWidth={2} aria-hidden />}
      {children}
    </button>
  );
}
