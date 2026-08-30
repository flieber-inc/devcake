import React, { useEffect } from "react";
import PageHeader from "../components/PageHeader.jsx";
import PoliciesSection from "../components/PoliciesSection.jsx";
import ProfilesSection from "../components/ProfilesSection.jsx";
import ScheduledTasksSection from "../components/ScheduledTasksSection.jsx";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";
import { SETTINGS_SECTIONS } from "../lib/nav.js";

const SUBTITLES = {
  policies: "Policy knobs gather into one draft — nothing applies until you Save",
  "scheduled-tasks":
    "Task settings apply on Save; Run now is immediate",
  profiles:
    "Everything here applies immediately — snapshots only capture already-saved settings",
};

// Settings dispatcher (CAKE-159): how the system behaves — Policies, Scheduled
// Tasks, Profiles & Export. One section per #/settings/<section> view. Per-view
// subtitle states the save regime honestly (Profiles is immediate; Policies and
// Scheduled Tasks ride the shared draft).
export default function SettingsPage({ section }) {
  const { dr, loadErr } = useSharedDraft();
  const meta = SETTINGS_SECTIONS.find((s) => s.id === section) || SETTINGS_SECTIONS[0];

  useEffect(() => {
    document.querySelector("main")?.scrollTo({ top: 0 });
  }, [section]);

  if (!dr.loaded) {
    return <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading…{loadErr}</p>;
  }

  return (
    <div className="space-y-5">
      <PageHeader title={meta.label} subtitle={SUBTITLES[section] || SUBTITLES.policies} />

      <div className="sticky top-0 z-20 -mx-4 flex flex-wrap gap-1.5 bg-surface/90 px-4 py-2 backdrop-blur dark:bg-surface-dark/90 lg:hidden">
        {SETTINGS_SECTIONS.map((s) => (
          <a key={s.id} href={`#/settings/${s.id}`}
            className={`shrink-0 rounded-full border px-3 py-1 text-xs font-medium ${
              section === s.id
                ? "border-accent-300 bg-accent-50 font-semibold text-accent-800 dark:border-accent-800 dark:bg-accent-950/60 dark:text-accent-200"
                : "border-neutral-200 bg-surface-raised text-neutral-600 dark:border-neutral-800 dark:bg-surface-raised-dark dark:text-neutral-300"
            }`}>
            {s.label}
          </a>
        ))}
      </div>

      {section === "policies" && <PoliciesSection />}
      {section === "scheduled-tasks" && <ScheduledTasksSection />}
      {section === "profiles" && <ProfilesSection />}
    </div>
  );
}
