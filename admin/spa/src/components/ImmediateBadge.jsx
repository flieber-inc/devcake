import React from "react";
import { Zap } from "lucide-react";

// Marks controls that act on the server the moment they're used — they do
// NOT wait for the Config page's Save.
export default function ImmediateBadge({ text = "applies immediately" }) {
  return (
    <span
      title="This action applies immediately — it does not wait for Save"
      className="inline-flex items-center gap-1 rounded bg-stone-100 px-1.5 py-0.5 text-[10px] font-medium text-neutral-500 dark:bg-neutral-800 dark:text-neutral-400"
    >
      <Zap size={10} aria-hidden />
      {text}
    </span>
  );
}
