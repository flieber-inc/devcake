import React, { useEffect, useState } from "react";
import PageHeader from "../components/PageHeader.jsx";
import AssignmentsSection from "../components/AssignmentsSection.jsx";
import DevTypesSection from "../components/DevTypesSection.jsx";
import LimitsSection from "../components/LimitsSection.jsx";
import ProfilesSection from "../components/ProfilesSection.jsx";
import PromptsSection from "../components/PromptsSection.jsx";
import SkillsSection from "../components/SkillsSection.jsx";
import ScheduledTasksSection from "../components/ScheduledTasksSection.jsx";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";
import { CONFIG_SECTIONS } from "../lib/nav.js";

// Settings dispatcher (Cursor/Codex style): one section per view, routed as
// #/config/<section>. Each section lives in src/components/<Name>Section.jsx
// and pulls the shared draft itself via useSharedDraft() — the draft, reload,
// harnesses and health snapshot come from the shared provider (v0.1.1 B4);
// the Repositories and PMO pages edit the SAME draft, and DraftChrome
// (App-level) owns Save/DirtyBar/NavGuard. PMO left this page for #/pmo
// under the Adapters item (2026-08-02 nav reorg).
export default function ConfigPage({ section, onHealthChange }) {
  const { dr, loadErr } = useSharedDraft();
  // page-level error line — sections report async failures here (delete /
  // restore flows) so the message survives their local re-renders
  const [pageErr, setPageErr] = useState("");

  const loaded = dr.loaded;

  // settings-style navigation: one section per view — switching sections
  // starts at the top of the pane
  useEffect(() => {
    document.querySelector("main")?.scrollTo({ top: 0 });
  }, [section]);

  if (!loaded) return <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading…{loadErr}</p>;

  return (
    <div className="space-y-5">
      <PageHeader title="Configuration"
        subtitle="Edits everywhere gather into one draft — nothing applies until you Save" />
      {pageErr && <p className="text-sm text-red-600 dark:text-red-400">✗ {pageErr}</p>}

      {/* mobile section switcher (sidebar sub-nav is expanded-drawer-only).
          Wrap — never horizontal-scroll (CAKE-162 / no-horizontal-scroll). */}
      <div className="sticky top-0 z-20 -mx-4 flex flex-wrap gap-1.5 bg-surface/90 px-4 py-2 backdrop-blur dark:bg-surface-dark/90 lg:hidden">
        {CONFIG_SECTIONS.map((s) => (
          <a key={s.id} href={`#/config/${s.id}`}
            className={`rounded-full border px-3 py-1 text-xs font-medium ${
              section === s.id
                ? "border-accent-300 bg-accent-50 font-semibold text-accent-800 dark:border-accent-800 dark:bg-accent-950/60 dark:text-accent-200"
                : "border-neutral-200 bg-surface-raised text-neutral-600 dark:border-neutral-800 dark:bg-surface-raised-dark dark:text-neutral-300"
            }`}>
            {s.label}
          </a>
        ))}
      </div>

      {section === "dev-types" && (
        <DevTypesSection setPageErr={setPageErr} onHealthChange={onHealthChange} />
      )}
      {section === "mission-types" && <AssignmentsSection />}
      {section === "skills" && <SkillsSection setPageErr={setPageErr} />}
      {section === "prompts" && (
        <PromptsSection cfg={dr.draft.cfg} setField={dr.setField}
          devTypeNames={Object.keys(dr.draft.devTypes || {})} />
      )}
      {section === "limits" && <LimitsSection />}
      {section === "scheduled-tasks" && <ScheduledTasksSection />}
      {section === "profiles" && <ProfilesSection />}
    </div>
  );
}
