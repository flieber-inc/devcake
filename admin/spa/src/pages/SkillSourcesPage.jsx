import React, { useState } from "react";
import PageHeader from "../components/PageHeader.jsx";
import ConnectionTabs from "../components/ConnectionTabs.jsx";
import SkillSourcesSection from "../components/SkillSourcesSection.jsx";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";
import { get } from "../api.js";

// Skill sources as their own Connections page (CAKE-159) — same Test-
// connection + credential shape as Repositories / PMO; edits ride the shared
// draft (DraftChrome owns Save/DirtyBar/NavGuard).
export default function SkillSourcesPage() {
  const { dr, loadErr } = useSharedDraft();
  const [pageErr, setPageErr] = useState("");

  if (!dr.loaded) {
    return <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading…{loadErr}</p>;
  }

  return (
    <div className="space-y-5">
      <PageHeader title="Skill sources"
        subtitle="External skill repositories and their tokens — edits apply on Save" />
      <ConnectionTabs page="skill-sources" />
      {pageErr && <p className="text-sm text-red-600 dark:text-red-400">✗ {pageErr}</p>}
      <SkillSourcesSection setPageErr={setPageErr}
        onCatalogReload={() => get("/skills").catch(() => {})} />
    </div>
  );
}
