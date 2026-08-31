// Unused-repo predicate — SPA mirror of
// app/devcake/api/health.py::unused_repo_names. A configured repo card is
// unused iff no PMO selects it as work, reference, or memory AND no Dev
// Type lists it in memory_repos. Skill-source prefixes (`source/skill`)
// name dedicated skill_sources, never repo cards — do not treat them as
// selections here.

export function unusedRepoNames(cfg, devTypes) {
  const selected = new Set();
  for (const p of cfg?.pmos || []) {
    for (const n of p.repos || []) selected.add(n);
    for (const n of p.reference_repos || []) selected.add(n);
    for (const n of p.memory_repos || []) selected.add(n);
  }
  for (const dt of Object.values(devTypes || {})) {
    for (const n of dt.memory_repos || []) selected.add(n);
  }
  // a repo card BACKING a skill source (ADR-0039) is in use — flagging it
  // unused would nudge a removal the config validator then refuses
  for (const s of cfg?.skill_sources || []) {
    if (s.backed_by) selected.add(s.backed_by);
  }
  return (cfg?.repos || []).map((r) => r.name).filter((n) => !selected.has(n));
}
