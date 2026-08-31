// Draft scaffolds for new instance cards. Every server-model field is
// present with its server default: a saved card must deep-equal its
// refetched server row, because two seams depend on that convergence —
// useNewNames' name-lock rule, and the rebase's clean-diff-after-save
// contract (a partial scaffold re-applied over the fresh snapshot strips
// the server's fields and leaves permanent phantom dirt).

export const newPmoCard = (name, system) => ({
  name,
  system,
  team_key: "",
  api_base: null,
  repos: [],
  reference_repos: [],
  memory_repos: [],
  intake_paused: false,
  discovery_routing: true,
  assignments: {},
  active_prompt_templates: {},
  managed: false,
});

export const newRepoCard = (name) => ({
  name,
  forge: "github",
  url: "",
  api_base: null,
  default_branch: "main",
  auto_merge: false,
  auto_resolve_merge_conflicts: true,
  merge_retry_window_minutes: 30,
  merge_settle_minutes: 0,
});

// dedicated skills connection (2026-08-14 ruling — never a repo card)
export const newSkillSourceCard = (name) => ({
  name,
  forge: "github",
  url: "",
  default_branch: "",
  subdir: "",
  backed_by: "",
});
