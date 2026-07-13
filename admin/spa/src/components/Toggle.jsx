import React from "react";

export default function Toggle({ on, onClick, disabled, label }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className={`h-6 w-11 shrink-0 rounded-full p-0.5 transition
        focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60
        disabled:opacity-40 ${on ? "bg-accent" : "bg-neutral-300 dark:bg-neutral-700"}`}
    >
      <span className={`block h-5 w-5 rounded-full bg-white shadow-sm transition ${on ? "translate-x-5" : ""}`} />
    </button>
  );
}
