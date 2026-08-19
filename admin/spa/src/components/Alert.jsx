import React from "react";
import { Info, TriangleAlert, OctagonAlert, X } from "lucide-react";

const STYLES = {
  info: {
    // quiet informational — neutral register, not a second brand accent
    box: "border-neutral-200 bg-neutral-50 text-neutral-900 dark:border-neutral-700 dark:bg-neutral-900/60 dark:text-neutral-200",
    icon: Info,
    iconCls: "text-neutral-500 dark:text-neutral-400",
  },
  warning: {
    box: "border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-200",
    icon: TriangleAlert,
    iconCls: "text-amber-500 dark:text-amber-400",
  },
  critical: {
    box: "border-red-200 bg-red-50 text-red-900 dark:border-red-900 dark:bg-red-950/60 dark:text-red-200",
    icon: OctagonAlert,
    iconCls: "text-red-500 dark:text-red-400",
  },
};

export default function Alert({ severity = "warning", title, body, onDismiss }) {
  const s = STYLES[severity] || STYLES.warning;
  const Icon = s.icon;
  return (
    <div className={`rounded-card border px-4 py-3 text-sm shadow-card ${s.box}`}>
      <div className="flex items-start gap-2.5">
        <Icon size={17} strokeWidth={2} className={`mt-0.5 shrink-0 ${s.iconCls}`} aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="font-medium">{title}</p>
          {body && (
            <p className="mt-0.5 opacity-90">
              {body}
            </p>
          )}
        </div>
        {onDismiss && (
          <button
            onClick={onDismiss}
            title="Dismiss this warning (it returns if its content changes)"
            aria-label="Dismiss"
            className="shrink-0 rounded p-1 opacity-60 transition hover:bg-black/5 hover:opacity-100 dark:hover:bg-white/10"
          >
            <X size={14} aria-hidden />
          </button>
        )}
      </div>
    </div>
  );
}
