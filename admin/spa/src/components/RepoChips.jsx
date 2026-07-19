import React from "react";
import SelectionChips from "./SelectionChips.jsx";

// Repo-flavored SelectionChips (PMO repo set + reference repos, v0.1.2):
// selection order is the list order; entries selected in the SIBLING list
// render disabled (the two sets are disjoint by config validation).
export default function RepoChips({ label, help, all, selected, excluded, excludedNote,
                     unavailable = [], unavailableNote = "",
                     firstBadge = "", onChange }) {
  return (
    <SelectionChips label={label} help={help}
      options={all.map((r) => ({
        name: r.name,
        disabled: excluded.includes(r.name) || unavailable.includes(r.name),
        disabledNote: unavailable.includes(r.name)
          ? unavailableNote
          : `already selected as a ${excludedNote}`,
      }))}
      selected={selected} onChange={onChange} firstBadge={firstBadge}
      emptyNote="no repositories configured — add them on the Repositories page"
      staleNote="this repo card no longer exists — click to remove the stale entry" />
  );
}
