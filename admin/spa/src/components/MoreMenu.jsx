import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ExternalLink, MoreHorizontal } from "lucide-react";

const GUTTER = 8;
const GAP = 4;

// Overflow menu (⋯) — the settings-panel idiom: one primary action stays
// visible, secondary/rare actions live here. The open menu is viewport-fixed
// so board lanes, drawers and table scrollers cannot clip it.
export default function MoreMenu({ label = "More actions", items }) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: GUTTER, top: GUTTER, maxHeight: 320 });
  const ref = useRef(null);
  const btnRef = useRef(null);
  const menuRef = useRef(null);

  const place = () => {
    const trigger = btnRef.current?.getBoundingClientRect();
    const menu = menuRef.current;
    if (!trigger || !menu) return;
    const width = menu.offsetWidth || 256;
    const maxHeight = Math.max(96, window.innerHeight - GUTTER * 2);
    const height = Math.min(menu.scrollHeight, maxHeight);
    const left = Math.min(
      Math.max(GUTTER, trigger.right - width),
      Math.max(GUTTER, window.innerWidth - width - GUTTER),
    );
    const below = trigger.bottom + GAP;
    const top = below + height <= window.innerHeight - GUTTER
      ? below
      : Math.max(GUTTER, trigger.top - height - GAP);
    setPosition({ left, top, maxHeight });
  };

  useLayoutEffect(() => {
    if (!open) return;
    place();
    const update = () => place();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [open, items.length]);

  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector('[role="menuitem"]')?.focus();
    const away = (e) => ref.current && !ref.current.contains(e.target) && setOpen(false);
    // Capture Escape before a containing modal sees it: first close the menu,
    // leave the drawer/dialog open, and return focus to the menu trigger.
    const esc = (e) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
      btnRef.current?.focus();
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc, true);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", esc, true);
    };
  }, [open]);

  const onKey = (e) => {
    if (!open) {
      if (e.key === "ArrowDown" && document.activeElement === btnRef.current) {
        e.preventDefault();
        setOpen(true);
      }
      return;
    }
    const focusable = [...(menuRef.current?.querySelectorAll('[role="menuitem"]') || [])];
    const index = focusable.indexOf(document.activeElement);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      focusable[(index + 1) % focusable.length]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      focusable[(index - 1 + focusable.length) % focusable.length]?.focus();
    } else if (e.key === "Home") {
      e.preventDefault();
      focusable[0]?.focus();
    } else if (e.key === "End") {
      e.preventDefault();
      focusable[focusable.length - 1]?.focus();
    } else if (e.key === "Tab") {
      setOpen(false);
      btnRef.current?.focus();
    }
  };

  return (
    <span ref={ref} onKeyDown={onKey} className="relative inline-block">
      <button
        ref={btnRef}
        type="button"
        title={label}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="flex h-11 w-11 items-center justify-center rounded-md border border-neutral-300 text-neutral-600 transition hover:bg-neutral-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:border-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-800 sm:h-8 sm:w-8"
      >
        <MoreHorizontal size={15} aria-hidden />
      </button>
      {open && (
        <div
          ref={menuRef}
          role="menu"
          aria-label={label}
          style={position}
          className="fixed z-50 w-64 overflow-y-auto overscroll-contain rounded-md border border-neutral-200 bg-white py-1 shadow-lg dark:border-neutral-700 dark:bg-neutral-900"
        >
          {items.map((it) => (
            <button
              key={it.label}
              type="button"
              role="menuitem"
              tabIndex={-1}
              onClick={() => { setOpen(false); it.onClick(); }}
              className="block min-h-11 w-full px-3 py-2 text-left text-sm transition hover:bg-stone-50 focus-visible:bg-stone-50 focus-visible:outline-none dark:hover:bg-neutral-800 dark:focus-visible:bg-neutral-800 sm:min-h-0"
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
