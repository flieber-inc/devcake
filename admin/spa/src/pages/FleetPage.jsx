import React, { useEffect, useState } from "react";
import PageHeader from "../components/PageHeader.jsx";
import AssignmentsSection from "../components/AssignmentsSection.jsx";
import DevTypesSection from "../components/DevTypesSection.jsx";
import PromptsSection from "../components/PromptsSection.jsx";
import SkillsSection from "../components/SkillsSection.jsx";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";
import { FLEET_SECTIONS } from "../lib/nav.js";

const SUBTITLES = {
  "dev-types":
    "Create/clone/delete apply immediately — editor fields gather into the draft and apply on Save",
  "mission-types":
    "Mission-type staffing edits apply on Save",
  prompts:
    "Templates apply immediately; the active selection saves with the page",
  skills:
    "Catalog add/delete/restore apply immediately — skill sources live under Connections",
};

// Fleet dispatcher (CAKE-159): who does the work — Dev Types, Mission Types,
// Prompts, Skills catalog. One section per #/fleet/<section> view. Edits that
// ride the draft share DraftChrome with Connections and Settings.
export default function FleetPage({ section, onHealthChange }) {
  const { dr, loadErr } = useSharedDraft();
  const [pageErr, setPageErr] = useState("");
  const meta = FLEET_SECTIONS.find((s) => s.id === section) || FLEET_SECTIONS[0];

  useEffect(() => {
    document.querySelector("main")?.scrollTo({ top: 0 });
  }, [section]);

  if (!dr.loaded) {
    return <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading…{loadErr}</p>;
  }

  return (
    <div className="space-y-5">
      <PageHeader title={meta.label} subtitle={SUBTITLES[section] || SUBTITLES["dev-types"]} />
      {pageErr && <p className="text-sm text-red-600 dark:text-red-400">✗ {pageErr}</p>}

      <div className="sticky top-0 z-20 -mx-4 flex flex-wrap gap-1.5 bg-surface/90 px-4 py-2 backdrop-blur dark:bg-surface-dark/90 lg:hidden">
        {FLEET_SECTIONS.map((s) => (
          <a key={s.id} href={`#/fleet/${s.id}`}
            className={`shrink-0 rounded-full border px-3 py-1 text-xs font-medium ${
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
      {section === "prompts" && (
        <PromptsSection cfg={dr.draft.cfg} setField={dr.setField}
          devTypeNames={Object.keys(dr.draft.devTypes || {})} />
      )}
      {section === "skills" && (
        <SkillsSection setPageErr={setPageErr}
          skillSources={dr.draft.cfg.skill_sources || []}
          repos={dr.draft.cfg.repos || []} />
      )}
    </div>
  );
}
