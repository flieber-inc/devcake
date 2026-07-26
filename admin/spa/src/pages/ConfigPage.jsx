import React, { useEffect, useState } from "react";
import PageHeader from "../components/PageHeader.jsx";
import AssignmentsSection from "../components/AssignmentsSection.jsx";
import DevTypesSection from "../components/DevTypesSection.jsx";
import LimitsSection from "../components/LimitsSection.jsx";
import PmoSection from "../components/PmoSection.jsx";
import ProfilesSection from "../components/ProfilesSection.jsx";
import PromptsSection from "../components/PromptsSection.jsx";
import SkillsSection from "../components/SkillsSection.jsx";
import TrafficSection from "../components/TrafficSection.jsx";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";
import { CONFIG_SECTIONS } from "../lib/nav.js";

// Settings dispatcher (Cursor/Codex style): one section per view, routed as
// #/config/<section>. Each section lives in src/components/<Name>Section.jsx
// and pulls the shared draft itself via useSharedDraft() — the draft, reload,
// harnesses and health snapshot come from the shared provider (v0.1.1 B4);
// the Repositories page edits the SAME draft, and DraftChrome (App-level)
// owns Save/DirtyBar/NavGuard.
export default function ConfigPage({ section, health, healthError, onHealthChange }) {
  const { dr, loadErr } = useSharedDraft();
  // page-level error line — sections report async failures here (delete /
  // restore flows) so the message survives their local re-renders
  const [pageErr, setPageErr] = useState("");
  // "new PMO card" name tracking lives HERE, not in PmoSection (audit D5 #12):
  // the dispatcher stays mounted across section switches, so a card added then
  // navigated-away-from keeps its editable-name status when the operator returns
  const pmoNewNames = useState(() => new Set());

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
        subtitle="One section at a time — drafted edits apply on Save, wherever you made them" />
      {pageErr && <p className="text-sm text-red-600 dark:text-red-400">✗ {pageErr}</p>}

      {/* mobile section switcher (sidebar sub-nav is expanded-drawer-only) */}
      <div className="sticky top-0 z-20 -mx-4 flex gap-1.5 overflow-x-auto bg-surface/90 px-4 py-2 backdrop-blur dark:bg-surface-dark/90 lg:hidden">
        {CONFIG_SECTIONS.map((s) => (
          <a key={s.id} href={`#/config/${s.id}`}
            className={`shrink-0 rounded-full border px-3 py-1 text-xs font-medium ${
              section === s.id
                ? "border-accent-300 bg-accent-50 font-semibold text-accent-800 dark:border-accent-800 dark:bg-accent-950/60 dark:text-accent-200"
                : "border-neutral-200 bg-surface-raised text-neutral-600 dark:border-neutral-800 dark:bg-surface-raised-dark dark:text-neutral-300"
            }`}>
            {s.label}
          </a>
        ))}
      </div>

      {section === "pmo" && (
        <PmoSection newNamesState={pmoNewNames}
          health={health} healthError={healthError} onHealthChange={onHealthChange} />
      )}
      {section === "dev-types" && <DevTypesSection setPageErr={setPageErr} />}
      {section === "skills" && <SkillsSection setPageErr={setPageErr} />}
      {section === "assignments" && <AssignmentsSection />}
      {section === "prompts" && (
        <PromptsSection cfg={dr.draft.cfg} setField={dr.setField}
          devTypeNames={Object.keys(dr.draft.devTypes || {})} />
      )}
      {section === "profiles" && <ProfilesSection />}
      {section === "limits" && <LimitsSection />}
      {section === "traffic" && <TrafficSection />}
    </div>
  );
}
