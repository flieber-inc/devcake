import React, { useEffect, useState } from "react";
import {
  LayoutDashboard, SquareTerminal, Settings2, FolderGit2, ScrollText,
  TriangleAlert, Sun, Moon, Monitor, Play, Pause, PanelLeftClose, PanelLeftOpen,
  Columns3, Briefcase,
} from "lucide-react";
import StatusDot from "./StatusDot.jsx";
import { SERVICES, serviceValue } from "../lib/services.js";
import Toggle from "./Toggle.jsx";
import { CONFIG_SECTIONS } from "../lib/nav.js";
import { getTheme, setTheme, onThemeChange } from "../theme.js";

// Entries are pages, or an Adapters-style group of pages. A group header is
// a STATIC label — never a button (tests/missions.mjs scans `aside button`
// for the collapse toggle) and never a link; collapsed mode hides it and
// renders the children as their own icon items so they stay reachable.
const NAV = [
  { page: "overview", href: "#/overview", label: "Overview", icon: LayoutDashboard },
  { page: "missions", href: "#/missions", label: "Missions", icon: Columns3 },
  { page: "runs", href: "#/runs", label: "Runs", icon: SquareTerminal },
  { group: "Adapters", children: [
    { page: "repos", href: "#/repos", label: "Repositories", icon: FolderGit2 },
    { page: "pmo", href: "#/pmo", label: "PMO", icon: Briefcase },
  ] },
  { page: "config", href: "#/config", label: "Configuration", icon: Settings2 },
  { page: "consoles", href: "#/consoles", label: "Consoles", icon: ScrollText },
];

function NavItem({ href, icon: Icon, label, active, collapsed, onClick }) {
  return (
    <a
      href={href}
      title={label}
      onClick={onClick}
      className={`flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm font-medium transition ${
        collapsed ? "justify-center" : ""
      } ${
        active
          ? "bg-accent-50 text-accent-800 dark:bg-accent-950/70 dark:text-accent-200"
          : "text-neutral-600 hover:bg-stone-100 hover:text-neutral-900 dark:text-neutral-400 dark:hover:bg-neutral-800 dark:hover:text-neutral-100"
      }`}
    >
      <Icon size={17} strokeWidth={2} className="shrink-0" aria-hidden />
      {!collapsed && <span>{label}</span>}
    </a>
  );
}

const THEME_OPTS = [
  { value: "light", icon: Sun, label: "Light" },
  { value: "dark", icon: Moon, label: "Dark" },
  { value: "system", icon: Monitor, label: "System" },
];

function ThemeToggle({ collapsed }) {
  const [pref, setPref] = useState(getTheme());
  useEffect(() => onThemeChange(setPref), []);
  if (collapsed) {
    const Current = THEME_OPTS.find((o) => o.value === pref).icon;
    const cycle = () => {
      const order = ["light", "dark", "system"];
      setTheme(order[(order.indexOf(pref) + 1) % 3]);
    };
    return (
      <button
        onClick={cycle}
        title={`Theme: ${pref}`}
        aria-label={`Theme: ${pref} (click to change)`}
        className="mx-auto flex h-8 w-8 items-center justify-center rounded-lg text-neutral-500 hover:bg-stone-100 dark:hover:bg-neutral-800"
      >
        <Current size={15} aria-hidden />
      </button>
    );
  }
  return (
    <div className="flex rounded-lg border border-neutral-200 p-0.5 dark:border-neutral-800">
      {THEME_OPTS.map(({ value, icon: Icon, label }) => (
        <button
          key={value}
          title={label}
          aria-label={`${label} theme`}
          onClick={() => setTheme(value)}
          className={`flex flex-1 items-center justify-center rounded-md py-1.5 transition ${
            pref === value
              ? "bg-stone-100 text-neutral-900 dark:bg-neutral-800 dark:text-neutral-100"
              : "text-neutral-500 dark:text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200"
          }`}
        >
          <Icon size={14} aria-hidden />
        </button>
      ))}
    </div>
  );
}

// The master switch: pauses/resumes mission intake for the whole system.
// Applies immediately (PUT /config) — deliberately NOT part of the Config draft.
function IntakeSwitch({ collapsed, paused, busy, disabled, error, onToggle }) {
  const unknown = paused === undefined || paused === null;
  const title = disabled
    ? "Mission intake — backend unreachable"
    : unknown
      ? "Mission intake — state unknown"
      : paused
        ? "Mission intake is PAUSED — click to resume"
        : "Mission intake is ON — click to pause";
  if (collapsed) {
    return (
      <button
        onClick={onToggle}
        disabled={disabled || busy || unknown}
        title={title}
        aria-label={title}
        className={`mx-auto flex h-9 w-9 items-center justify-center rounded-lg border transition disabled:opacity-40 ${
          paused
            ? "border-amber-300 bg-amber-100 text-amber-800 hover:bg-amber-200 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300"
            : "border-green-200 bg-green-50 text-green-700 hover:bg-green-100 dark:border-green-900 dark:bg-green-950/60 dark:text-green-400"
        }`}
      >
        {paused ? <Pause size={16} aria-hidden /> : <Play size={16} aria-hidden />}
      </button>
    );
  }
  return (
    <div
      className={`rounded-lg border p-3 ${
        paused
          ? "border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/60"
          : "border-neutral-200 dark:border-neutral-800"
      }`}
    >
      <p className="text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
        Mission intake
      </p>
      <div className="mt-2 flex items-center gap-2.5">
        <Toggle
          on={!paused && !unknown}
          disabled={disabled || busy || unknown}
          label="Mission intake"
          onClick={onToggle}
        />
        <span className={`text-sm font-semibold ${
          unknown ? "text-neutral-500 dark:text-neutral-400"
            : paused ? "text-amber-700 dark:text-amber-300" : "text-green-700 dark:text-green-400"
        }`}>
          {unknown ? "unknown" : paused ? "PAUSED" : "ON"}
        </span>
      </div>
      <p className="mt-1.5 text-[11px] leading-snug text-neutral-500 dark:text-neutral-400">
        {error
          ? <span className="text-red-600 dark:text-red-400">{error}</span>
          : paused
            ? "No new runs start; in-flight runs finish normally."
            : "Applies immediately — the whole system's master switch."}
      </p>
    </div>
  );
}

export default function Sidebar({
  page, configSection, alertCount, health, healthError,
  intakePaused, intakeBusy, intakeError, onIntakeToggle,
}) {
  const [collapsedPref, setCollapsedPref] = useState(() => {
    const v = localStorage.getItem("devcake-sidebar");
    if (v === "collapsed") return true;
    if (v === "expanded") return false;
    return !window.matchMedia("(min-width: 1024px)").matches;
  });
  const toggleCollapsed = () => {
    setCollapsedPref((c) => {
      localStorage.setItem("devcake-sidebar", !c ? "collapsed" : "expanded");
      return !c;
    });
  };
  // Missions kanban needs ≥1440 to fit 7 cols; force-collapse below that on
  // this page only, without touching the persisted preference.
  const [narrowForMissions, setNarrowForMissions] = useState(false);
  useEffect(() => {
    if (page !== "missions") { setNarrowForMissions(false); return; }
    const mq = window.matchMedia("(max-width: 1439.98px)");
    const update = () => setNarrowForMissions(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, [page]);
  const collapsed = collapsedPref || narrowForMissions;

  const dotOk = (key) =>
    (healthError && key === "app" ? false : serviceValue(health, key));
  const anyDown = healthError || SERVICES.some(([k]) => dotOk(k) === false);
  const allKnown = !healthError &&
    SERVICES.every(([k]) => serviceValue(health, k) !== undefined);

  return (
    <aside
      className={`flex shrink-0 flex-col border-r border-neutral-200 bg-surface-raised transition-[width] duration-200 dark:border-neutral-800 dark:bg-surface-raised-dark ${
        collapsed ? "w-14" : "w-60"
      }`}
    >
      <a href="#/overview" className={`flex items-center gap-2.5 px-3 py-4 ${collapsed ? "justify-center" : "px-4"}`}>
        {/* the app mark is the signature layer-cake, mid-bake */}
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent shadow-card">
          <span className="flex w-3.5 flex-col-reverse gap-[2.5px]" aria-hidden>
            <span className="h-[3px] rounded-full bg-white/60" />
            <span className="h-[3px] rounded-full bg-white/60" />
            <span className="h-[3px] rounded-full bg-white" />
            <span className="h-[3px] rounded-full bg-white/25" />
          </span>
        </span>
        {!collapsed && (
          <span className="leading-tight">
            <span className="block font-display text-sm font-extrabold tracking-tight">DevCake</span>
            <span className="block text-[10px] text-neutral-500 dark:text-neutral-400">agentic developer</span>
          </span>
        )}
      </a>

      <div className={collapsed ? "px-2 pb-2" : "px-3 pb-2"}>
        <IntakeSwitch
          collapsed={collapsed}
          paused={intakePaused}
          busy={intakeBusy}
          disabled={healthError}
          error={intakeError}
          onToggle={onIntakeToggle}
        />
      </div>

      {/* min-h-0 + overflow-y-auto: the Adapters group's always-visible
          children cost vertical space — the mt-auto footer (theme, service
          grid, collapse) must never clip at short viewport heights */}
      <nav className={`flex min-h-0 flex-col gap-0.5 overflow-y-auto pt-1 ${collapsed ? "px-2" : "px-3"}`}>
        {NAV.map((item) => item.group ? (
          <React.Fragment key={item.group}>
            {!collapsed && (
              <p className="px-2.5 pb-0.5 pt-2 text-[10px] font-semibold uppercase tracking-widest text-neutral-400 dark:text-neutral-500">
                {item.group}
              </p>
            )}
            {item.children.map((child) => (
              <NavItem key={child.page} {...child}
                active={page === child.page} collapsed={collapsed} />
            ))}
          </React.Fragment>
        ) : (
          <React.Fragment key={item.page}>
            <NavItem {...item} active={page === item.page} collapsed={collapsed} />
            {item.page === "config" && page === "config" && !collapsed && (
              <div className="ml-[1.35rem] flex flex-col gap-0.5 border-l border-neutral-200 py-1 pl-3 dark:border-neutral-800">
                {CONFIG_SECTIONS.map((s) => (
                  <a
                    key={s.id}
                    href={`#/config/${s.id}`}
                    className={`rounded-md px-2 py-1 text-xs transition ${
                      configSection === s.id
                        ? "font-semibold text-accent-700 dark:text-accent-300"
                        : "text-neutral-500 hover:text-neutral-900 dark:text-neutral-400 dark:hover:text-neutral-100"
                    }`}
                  >
                    {s.label}
                  </a>
                ))}
              </div>
            )}
          </React.Fragment>
        ))}
      </nav>

      {alertCount > 0 && (
        <a
          href="#/overview"
          title={`${alertCount} active warning${alertCount > 1 ? "s" : ""}`}
          className={`mt-3 flex items-center justify-center gap-1.5 rounded-lg border border-amber-200 bg-amber-50 px-2 py-1.5 text-xs font-semibold text-amber-800 hover:bg-amber-100 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300 dark:hover:bg-amber-950 ${
            collapsed ? "mx-2" : "mx-3"
          }`}
        >
          <TriangleAlert size={13} aria-hidden />
          {collapsed ? alertCount : `${alertCount} alert${alertCount > 1 ? "s" : ""}`}
        </a>
      )}

      <div className={`mt-auto space-y-3 pb-3 ${collapsed ? "px-2" : "px-3"}`}>
        <ThemeToggle collapsed={collapsed} />
        {collapsed ? (
          <div
            className="flex justify-center"
            title={healthError ? "backend unreachable" : anyDown ? "a service is down" : allKnown ? "all services healthy" : "health unknown"}
          >
            <span className={`h-2 w-2 rounded-full ${anyDown ? "bg-red-500" : allKnown ? "bg-green-500" : "bg-neutral-400"}`} />
          </div>
        ) : (
          <div className={`grid grid-cols-3 gap-x-3 gap-y-1 rounded-lg border px-2.5 py-2 ${
            healthError
              ? "border-red-300 dark:border-red-900"
              : "border-neutral-200 dark:border-neutral-800"
          }`}>
            {SERVICES.map(([key, label]) => (
              <span key={key} className={healthError && key !== "app" ? "opacity-50" : ""}>
                <StatusDot ok={dotOk(key)} label={label} />
              </span>
            ))}
            {healthError && (
              <span className="col-span-3 text-[10px] font-medium text-red-600 dark:text-red-400">
                backend unreachable
              </span>
            )}
          </div>
        )}
        {!collapsed && (
          <p className="text-[10px] text-neutral-500 dark:text-neutral-400">
            DevCake v0 ·{" "}
            <a className="underline" href="https://github.com/fidecastro/devcake" target="_blank" rel="noopener">
              spec &amp; source
            </a>
          </p>
        )}
        {/* When Missions force-collapses under 1440, toggling would just flip
           the persisted preference without any visible change (effective
           stays collapsed) — a control that lies about being functional.
           Disable it with an honest title instead. */}
        <button
          onClick={toggleCollapsed}
          disabled={narrowForMissions}
          title={narrowForMissions
            ? "Sidebar auto-collapsed on Missions below 1440 px so the board fits"
            : collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-label={narrowForMissions
            ? "Sidebar auto-collapsed on Missions below 1440 px"
            : collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className={`flex items-center gap-2 rounded-lg py-1.5 text-xs text-neutral-500 dark:text-neutral-400 hover:bg-stone-100 hover:text-neutral-700 dark:hover:bg-neutral-800 dark:hover:text-neutral-200 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-neutral-500 dark:disabled:hover:bg-transparent dark:disabled:hover:text-neutral-400 ${
            collapsed ? "mx-auto h-8 w-8 justify-center" : "w-full px-2.5"
          }`}
        >
          {collapsed ? <PanelLeftOpen size={15} aria-hidden /> : <PanelLeftClose size={15} aria-hidden />}
          {!collapsed && "Collapse"}
        </button>
      </div>
    </aside>
  );
}
