// Instance-qualified mission identity (ADR-0009). gitea_issues pmo_ids
// are per-repo issue numbers, so a bare id collides across boards.

export function refKey(instance, pmoId) {
  return `${instance}:${pmoId}`;
}

export function runsListPath(mission) {
  const key = encodeURIComponent(mission.key || "");
  const inst = encodeURIComponent(mission.instance || "");
  return `/runs?limit=100&mission_key=${key}&pmo_ref=${inst}`;
}

export function liveMission(rows, open) {
  if (!open) return null;
  const hit = (rows || []).find(
    (m) => m.instance === open.instance && m.pmo_id === open.pmo_id,
  );
  return hit || open;
}
