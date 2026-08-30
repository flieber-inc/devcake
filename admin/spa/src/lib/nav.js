// Fleet / Settings section anchors — shared by the sidebar sub-nav, the
// mobile chip rows, and the Fleet/Settings dispatchers. Array order is
// load-bearing: the first Fleet entry is where bare #/fleet (and legacy
// #/config) lands. Connection pages (repos / pmo / skill-sources) are
// top-level hashes under the Connections nav item, not sections here
// (CAKE-159; mirrors the 2026-08-02 Adapters pull-out).

export const FLEET_SECTIONS = [
  { id: "dev-types", label: "Dev Types" },
  { id: "mission-types", label: "Mission Types" },
  { id: "prompts", label: "Prompts" },
  { id: "skills", label: "Skills" },
];

export const SETTINGS_SECTIONS = [
  { id: "limits", label: "Limits" },
  { id: "scheduled-tasks", label: "Scheduled Tasks" },
  { id: "profiles", label: "Profiles & Export" },
];

/** @deprecated Prefer FLEET_SECTIONS / SETTINGS_SECTIONS — kept for any
 *  leftover import during the CAKE-159 cutover. */
export const CONFIG_SECTIONS = [
  ...FLEET_SECTIONS,
  ...SETTINGS_SECTIONS,
];

export const CONNECTION_PAGES = [
  { page: "repos", href: "#/repos", label: "Repositories" },
  { page: "pmo", href: "#/pmo", label: "PMO" },
  { page: "skill-sources", href: "#/skill-sources", label: "Skill sources" },
];

export const DRAFT_PAGES = [
  "repos", "pmo", "skill-sources", "fleet", "settings",
];
