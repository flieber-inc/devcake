import React, { useEffect, useRef, useState } from "react";
import { TriangleAlert, OctagonAlert } from "lucide-react";
import Sidebar from "./components/Sidebar.jsx";
import ActivityBar from "./components/ActivityBar.jsx";
import OverviewPage, { SETUP_INTERNAL_FORGE_KEY } from "./pages/OverviewPage.jsx";
import FleetPage from "./pages/FleetPage.jsx";
import SettingsPage from "./pages/SettingsPage.jsx";
import ReposPage from "./pages/ReposPage.jsx";
import RunsPage from "./pages/RunsPage.jsx";
import MissionsPage from "./pages/MissionsPage.jsx";
import DraftChrome from "./components/DraftChrome.jsx";
import { ConfigDraftProvider } from "./lib/ConfigDraftContext.jsx";
import PmoPage from "./pages/PmoPage.jsx";
import SkillSourcesPage from "./pages/SkillSourcesPage.jsx";
import ConsolesPage from "./pages/ConsolesPage.jsx";
import deriveAlerts, { alertKey } from "./lib/alerts.js";
import {
  DRAFT_PAGES, FLEET_SECTIONS, SETTINGS_SECTIONS,
} from "./lib/nav.js";
import usePoll from "./lib/usePoll.js";
import { get, send } from "./api.js";

const PAGES = [
  "overview", "missions", "runs",
  "repos", "pmo", "skill-sources",
  "fleet", "settings", "consoles",
];
const FLEET_IDS = FLEET_SECTIONS.map((s) => s.id);
const SETTINGS_IDS = SETTINGS_SECTIONS.map((s) => s.id);

// Renamed/moved routes (2026-08-02 Adapters reorg + CAKE-159 Connections /
// Fleet / Settings): old bookmarks land on their new homes, never silently
// on the first section via the unknown-section fallback below.
const REDIRECTS = [
  [/^#\/config\/repository$/, "#/repos", { page: "repos", section: null }],
  [/^#\/config\/pmo$/, "#/pmo", { page: "pmo", section: null }],
  [/^#\/config\/skills$/, "#/fleet/skills",
    { page: "fleet", section: "skills" }],
  [/^#\/config\/dev-types$/, "#/fleet/dev-types",
    { page: "fleet", section: "dev-types" }],
  [/^#\/config\/mission-types$/, "#/fleet/mission-types",
    { page: "fleet", section: "mission-types" }],
  [/^#\/config\/prompts$/, "#/fleet/prompts",
    { page: "fleet", section: "prompts" }],
  [/^#\/config\/limits$/, "#/settings/policies",
    { page: "settings", section: "policies" }],
  [/^#\/config\/policies$/, "#/settings/policies",
    { page: "settings", section: "policies" }],
  [/^#\/config\/scheduled-tasks$/, "#/settings/scheduled-tasks",
    { page: "settings", section: "scheduled-tasks" }],
  [/^#\/config\/profiles$/, "#/settings/profiles",
    { page: "settings", section: "profiles" }],
  [/^#\/config\/traffic$/, "#/settings/policies",
    { page: "settings", section: "policies" }],
  [/^#\/config\/cron$/, "#/settings/scheduled-tasks",
    { page: "settings", section: "scheduled-tasks" }],
  [/^#\/config\/assignments$/, "#/fleet/mission-types",
    { page: "fleet", section: "mission-types" }],
  [/^#\/config\/?$/, "#/fleet/dev-types",
    { page: "fleet", section: "dev-types" }],
  [/^#\/config\/.+$/, "#/fleet/dev-types",
    { page: "fleet", section: "dev-types" }],
  [/^#\/logs$/, "#/consoles", { page: "consoles", section: null }],
];

// tiny hash router: #/overview · #/runs · #/fleet/<section> ·
// #/settings/<section> · #/repos · #/pmo · #/skill-sources · #/consoles.
// Fleet and Settings are settings-style — one section per view — so bare
// #/fleet or #/settings (or an unknown section) lands on the first section.
// Redirects replace the hash (no history entry) and return a synthetic route
// so the render doesn't wait for the hashchange round-trip.
function parseHash() {
  for (const [re, target, route] of REDIRECTS) {
    if (re.test(window.location.hash)) {
      window.location.replace(target);
      return route;
    }
  }
  const m = window.location.hash.match(/^#\/([^/]+)(?:\/(.+))?/);
  const page = m && PAGES.includes(m[1]) ? m[1] : "overview";
  // Peel ?query / #fragment off the section segment before allowlist checks
  // so deep-link flags like #/fleet/skills?add=1 stay on Skills (CAKE-165).
  const sectionId = (raw) => (raw || "").split(/[?#]/, 1)[0] || "";
  if (page === "fleet") {
    const sec = sectionId(m?.[2]);
    if (!sec || !FLEET_IDS.includes(sec)) {
      window.location.replace(`#/fleet/${FLEET_IDS[0]}`);
      return { page: "fleet", section: FLEET_IDS[0] };
    }
    return { page, section: sec };
  }
  if (page === "settings") {
    const sec = sectionId(m?.[2]);
    if (!sec || !SETTINGS_IDS.includes(sec)) {
      window.location.replace(`#/settings/${SETTINGS_IDS[0]}`);
      return { page: "settings", section: SETTINGS_IDS[0] };
    }
    return { page, section: sec };
  }
  return { page, section: null };
}

export default function App() {
  const [route, setRoute] = useState(parseHash);
  const [health, setHealth] = useState({});
  const [healthError, setHealthError] = useState(false);
  const [lastOkAt, setLastOkAt] = useState(null);
  const [intakeOverride, setIntakeOverride] = useState(null);
  const [intakeBusy, setIntakeBusy] = useState(false);
  const [intakeError, setIntakeError] = useState("");
  // dismissed advisory alerts: server-persisted (config.dismissed_alerts),
  // localStorage as fallback while the PUT can't reach the backend
  const [dismissed, setDismissed] = useState(() => {
    try { return JSON.parse(localStorage.getItem("devcake-dismissed")) || []; }
    catch { return []; }
  });
  useEffect(() => {
    get("/config")
      .then((c) => setDismissed((local) =>
        [...new Set([...(c.dismissed_alerts || []), ...local])]))
      .catch(() => {});
  }, []);
  const persistDismissed = async (list) => {
    setDismissed(list);
    try {
      await send("PUT", "/config", { dismissed_alerts: list });
      localStorage.removeItem("devcake-dismissed");
    } catch {
      localStorage.setItem("devcake-dismissed", JSON.stringify(list));
    }
  };

  // nav guard: DraftChrome registers an async fn while its draft is dirty.
  // hashchange fires AFTER the URL already changed, so blocking = revert the
  // hash (one suppressed event), ask, then replay the target if allowed.
  const navGuard = useRef(null);
  const lastHash = useRef(window.location.hash || "#/overview");
  const suppress = useRef(false);
  useEffect(() => {
    const onHash = async () => {
      const targetHash = window.location.hash;
      if (suppress.current) {
        suppress.current = false;
        lastHash.current = targetHash;
        setRoute(parseHash());
        return;
      }
      const next = parseHash();
      const leavingDraft = /^#\/(fleet|settings|repos|pmo|skill-sources)/.test(lastHash.current)
        && !DRAFT_PAGES.includes(next.page);
      if (navGuard.current && leavingDraft) {
        suppress.current = true;
        window.location.hash = lastHash.current; // revert before asking
        const proceed = await navGuard.current();
        if (proceed) {
          suppress.current = true;
          window.location.hash = targetHash;
        }
        return;
      }
      lastHash.current = targetHash;
      setRoute(next);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const registerNavGuard = (fn) => { navGuard.current = fn; };

  // honest health: keep last-known data on failure; the failure itself is the signal
  usePoll(
    () =>
      get("/health")
        .then((h) => { setHealth(h); setHealthError(false); setLastOkAt(new Date()); })
        .catch(() => setHealthError(true)),
    10000
  );

  const intakePaused = intakeOverride ?? health.intake_paused;
  const toggleIntake = async () => {
    if (intakeBusy || healthError || health.intake_paused === undefined) return;
    const next = !intakePaused;
    setIntakeOverride(next);
    setIntakeBusy(true);
    setIntakeError("");
    try {
      await send("PUT", "/config", { intake_paused: next });
      setHealth((h) => ({ ...h, intake_paused: next }));
    } catch (e) {
      setIntakeError(`✗ ${String(e.message || e)}`);
      setTimeout(() => setIntakeError(""), 5000);
    } finally {
      setIntakeOverride(null);
      setIntakeBusy(false);
    }
  };

  const allAlerts = deriveAlerts(health);
  const dismissedSet = new Set(dismissed);
  const alerts = allAlerts.filter((a) => !dismissedSet.has(alertKey(a)));
  const dismissedAlerts = allAlerts.filter((a) => dismissedSet.has(alertKey(a)));
  const dismissAlert = (a) => persistDismissed([...dismissed, alertKey(a)]);
  const restoreAlert = (a) => persistDismissed(dismissed.filter((k) => k !== alertKey(a)));
  const critical = alerts.filter((a) => a.severity === "critical");
  const { page, section } = route;

  return (
    <ConfigDraftProvider health={health}>
    <div className="flex h-screen bg-surface text-neutral-900 dark:bg-surface-dark dark:text-neutral-100">
      <Sidebar
        page={page}
        section={section}
        alertCount={alerts.length}
        health={health}
        healthError={healthError}
        intakePaused={intakePaused}
        intakeBusy={intakeBusy}
        intakeError={intakeError}
        onIntakeToggle={toggleIntake}
      />
      <div className="flex min-w-0 flex-1 flex-col">
        {healthError && (
          <div className="flex items-center gap-2 border-b border-red-300 bg-red-50 px-4 py-1.5 text-xs font-medium text-red-800 dark:border-red-900 dark:bg-red-950/80 dark:text-red-200 sm:px-8">
            <OctagonAlert size={13} className="shrink-0" aria-hidden />
            <span className="truncate">
              Backend unreachable —{" "}
              {lastOkAt
                ? `showing data from ${lastOkAt.toLocaleTimeString()}`
                : "no data received yet"}
              . Actions will fail until it responds.
            </span>
          </div>
        )}
        {/* safety warnings stay visible on every page: slim strip → Overview */}
        {alerts.length > 0 && page !== "overview" && (
          <a
            href="#/overview"
            className="flex items-center gap-2 border-b border-amber-200 bg-amber-50 px-4 py-1.5 text-xs text-amber-900 hover:bg-amber-100 dark:border-amber-900 dark:bg-amber-950/70 dark:text-amber-200 dark:hover:bg-amber-950 sm:px-8"
          >
            <TriangleAlert size={13} className="shrink-0" aria-hidden />
            <span className="truncate">
              {alerts.length} active warning{alerts.length > 1 ? "s" : ""}
              {critical.length > 0 && <> — {critical.map((a) => a.title).join(" · ")}</>}
            </span>
            <span className="ml-auto shrink-0 font-medium underline underline-offset-2">
              view on Overview
            </span>
          </a>
        )}
        <main className="flex-1 overflow-y-auto px-4 py-6 sm:px-8">
          <div className="mx-auto w-full max-w-6xl">
            {page === "overview" && (
              <OverviewPage
                health={health}
                alerts={alerts}
                dismissedAlerts={dismissedAlerts}
                onDismissAlert={dismissAlert}
                onRestoreAlert={restoreAlert}
                dismissedKeys={dismissed}
                onDismissInternalForge={() => {
                  if (dismissed.includes(SETUP_INTERNAL_FORGE_KEY)) return;
                  persistDismissed([...dismissed, SETUP_INTERNAL_FORGE_KEY]);
                }}
              />
            )}
            {page === "fleet" && (
              <FleetPage section={section} onHealthChange={setHealth} />
            )}
            {page === "settings" && <SettingsPage section={section} />}
            {page === "repos" && <ReposPage onHealthChange={setHealth} />}
            {page === "pmo" && (
              <PmoPage health={health} healthError={healthError}
                onHealthChange={setHealth} />
            )}
            {page === "skill-sources" && <SkillSourcesPage />}
            {page === "runs" && <RunsPage />}
            {page === "missions" && <MissionsPage />}
            {page === "consoles" && <ConsolesPage health={health} />}
            {DRAFT_PAGES.includes(page) && (
              <DraftChrome registerNavGuard={registerNavGuard} health={health} />
            )}
          </div>
        </main>
        <ActivityBar />
      </div>
    </div>
    </ConfigDraftProvider>
  );
}
