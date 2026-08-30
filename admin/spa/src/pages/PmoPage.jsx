import React from "react";
import PageHeader from "../components/PageHeader.jsx";
import PmoSection from "../components/PmoSection.jsx";
import ConnectionTabs from "../components/ConnectionTabs.jsx";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";

// PMO connections as their own page under the sidebar's Connections item
// (CAKE-159; was Adapters) — edits ride the SAME unified draft as
// Repositories, Skill sources, Fleet, and Settings (DraftChrome owns
// Save/DirtyBar/NavGuard; App gates it on this page too).
export default function PmoPage({ health, healthError, onHealthChange }) {
  const { dr, loadErr, pmoNewNamesState } = useSharedDraft();

  if (!dr.loaded) {
    return <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading…{loadErr}</p>;
  }

  return (
    <div className="space-y-5">
      <PageHeader title="PMO"
        subtitle="Project-management connections DevCake polls for missions — edits apply on Save" />
      <ConnectionTabs page="pmo" />
      <PmoSection newNamesState={pmoNewNamesState}
        health={health} healthError={healthError}
        onHealthChange={onHealthChange} />
    </div>
  );
}
