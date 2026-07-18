import React, { useEffect, useRef, useState } from "react";
import { ExternalLink, MoreHorizontal } from "lucide-react";

// Overflow menu (⋯) — the settings-panel idiom: one primary action stays
// visible, secondary/rare actions live here. items: [{ label, desc?,
// external?, danger?, onClick }] — `external` renders the ↗ marker,
// `danger` renders the label red (destructive actions still confirm).
export default function MoreMenu({ label = "More actions", items }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const away = (e) => ref.current && !ref.current.contains(e.target) && setOpen(false);
    const esc = (e) => e.key === "Escape" && setOpen(false);
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  return (
    <span ref={ref} className="relative inline-block">
      <button
        type="button"
        title={label}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="flex h-8 w-8 items-center justify-center rounded-md border border-neutral-300 text-neutral-600 transition hover:bg-neutral-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:border-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-800"
      >
        <MoreHorizontal size={15} aria-hidden />
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full z-40 mt-1 w-64 rounded-md border border-neutral-200 bg-white py-1 shadow-lg dark:border-neutral-700 dark:bg-neutral-900"
        >
          {items.map((it) => (
            <button
              key={it.label}
              type="button"
              role="menuitem"
              onClick={() => { setOpen(false); it.onClick(); }}
              className="block w-full px-3 py-2 text-left text-sm transition hover:bg-stone-50 focus-visible:bg-stone-50 focus-visible:outline-none dark:hover:bg-neutral-800 dark:focus-visible:bg-neutral-800"
            >
              <span className={`flex items-center gap-1.5 font-medium ${
                it.danger ? "text-red-600 dark:text-red-400" : ""}`}>
                {it.label}
                {it.external && <ExternalLink size={11} className="text-neutral-500 dark:text-neutral-400" aria-hidden />}
              </span>
              {it.desc && (
                <span className="mt-0.5 block text-xs font-normal text-neutral-500 dark:text-neutral-400">
                  {it.desc}
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </span>
  );
}
