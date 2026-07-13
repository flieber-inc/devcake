import React, { useState } from "react";
import { Info, TriangleAlert, OctagonAlert, ChevronRight, ExternalLink, X } from "lucide-react";

const STYLES = {
  info: {
    box: "border-sky-200 bg-sky-50 text-sky-900 dark:border-sky-900 dark:bg-sky-950/60 dark:text-sky-200",
    icon: Info,
    iconCls: "text-sky-500 dark:text-sky-400",
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

// items entries are "text — url"; render the trailing url as a link
function Item({ entry }) {
  const i = entry.lastIndexOf(" — ");
  const [text, url] = i > -1 ? [entry.slice(0, i), entry.slice(i + 3)] : [entry, null];
  return (
    <li className="list-disc">
      {text}
      {url && (
        <>
          {" — "}
          <a href={url} target="_blank" rel="noreferrer" className="underline underline-offset-2">
            {url}
          </a>
        </>
      )}
    </li>
  );
}

export default function Alert({ severity = "warning", title, body, items, collapsible, link, onDismiss }) {
  const [open, setOpen] = useState(true);
  const s = STYLES[severity] || STYLES.warning;
  const Icon = s.icon;
  return (
    <div className={`rounded-card border px-4 py-3 text-sm shadow-card ${s.box}`}>
      <div className="flex items-start gap-2.5">
        <Icon size={17} strokeWidth={2} className={`mt-0.5 shrink-0 ${s.iconCls}`} aria-hidden />
        <div className="min-w-0 flex-1">
          {collapsible ? (
            <button className="flex w-full items-center gap-2 text-left font-medium"
              onClick={() => setOpen(!open)}>
              <span>{title}</span>
              <ChevronRight size={13} className={`transition-transform ${open ? "rotate-90" : ""}`} aria-hidden />
            </button>
          ) : (
            <p className="font-medium">{title}</p>
          )}
          {body && (
            <p className="mt-0.5 opacity-90">
              {body}
              {link && (
                <>
                  {" "}
                  <a href={link.href} className="inline-flex items-center gap-0.5 font-medium underline underline-offset-2">
                    {link.label}
                    {link.href.startsWith("http") && <ExternalLink size={11} aria-hidden />}
                  </a>
                </>
              )}
            </p>
          )}
          {items && open && (
            <ul className="mt-1.5 space-y-0.5 pl-5">
              {items.map((e) => <Item key={e} entry={e} />)}
            </ul>
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
