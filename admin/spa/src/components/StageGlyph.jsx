import React from "react";

const STAGES = ["ONBOARD", "PLAN", "EXECUTE", "REVIEW"];

// The layer cake: the mission pipeline's strict 4-stage sequence as stacked
// layers, baked bottom-up. Filled = stages before this run, accent = the
// stage this run executes. Purely informational — it sits beside the status
// pill and adds the "how far along" axis the pill doesn't carry.
export default function StageGlyph({ stage, size = 14 }) {
  const idx = STAGES.indexOf(stage);
  if (idx === -1) return null;
  const label = `stage ${idx + 1} of 4 — ${stage}`;
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className="inline-flex shrink-0 flex-col-reverse justify-center"
      style={{ width: size, gap: Math.max(1.5, size / 8) }}
    >
      {STAGES.map((s, i) => (
        <span
          key={s}
          style={{ height: Math.max(2, Math.round(size / 6)) }}
          className={`rounded-full ${
            i < idx
              ? "bg-neutral-400 dark:bg-neutral-600"
              : i === idx
                ? "bg-accent-600 dark:bg-accent-400"
                : "bg-neutral-200 dark:bg-neutral-800"
          }`}
        />
      ))}
    </span>
  );
}
