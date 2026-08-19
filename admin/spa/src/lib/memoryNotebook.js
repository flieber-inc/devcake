// Memory-notebook binding for a repository card — same selection sets as
// unusedRepos.js / health.unused_repo_names: PMO memory_repos ∪ Dev Type
// memory_repos. Skill-source prefixes are not notebook binding.

export function isMemoryNotebookCard(name, cfg, devTypes) {
  if (!name) return false;
  for (const p of cfg?.pmos || []) {
    if ((p.memory_repos || []).includes(name)) return true;
  }
  for (const dt of Object.values(devTypes || {})) {
    if ((dt.memory_repos || []).includes(name)) return true;
  }
  return false;
}
