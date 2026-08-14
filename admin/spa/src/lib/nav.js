// Config page section anchors — shared by the sidebar sub-nav, the mobile
// chip row, and the ConfigPage scrollspy. Array order is load-bearing: the
// first entry is where bare #/config lands. PMO is NOT here — it lives as
// its own page (#/pmo) under the sidebar's Adapters item (2026-08-02).
export const CONFIG_SECTIONS = [
  { id: "dev-types", label: "Dev Types" },
  { id: "mission-types", label: "Mission Types" },
  { id: "skills", label: "Skills" },
  { id: "prompts", label: "Prompts" },
  { id: "limits", label: "Limits" },
  { id: "scheduled-tasks", label: "Scheduled Tasks" },
  { id: "profiles", label: "Profiles & Export" },
];
