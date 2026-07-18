import React from "react";
import { Zap } from "lucide-react";

// Spatial convention for the two persistence regimes: anything inside a
// crust-tinted zone hits the server the moment it's used — plain zones ride
// the unified draft and wait for the page-level Save.
export default function InstantZone({ children, note = "applies immediately — does not wait for Save", className = "" }) {
  return (
    <div className={`space-y-2 rounded-md border border-accent-200 bg-accent-50/50 p-3 dark:border-accent-900 dark:bg-accent-950/20 ${className}`}>
      <p className="flex items-center gap-1 font-mono text-[10px] font-medium uppercase tracking-widest text-accent-700 dark:text-accent-300">
        <Zap size={10} aria-hidden /> {note}
      </p>
      {children}
    </div>
  );
}
