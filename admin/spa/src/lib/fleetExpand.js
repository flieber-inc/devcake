// Fleet card expand-on-load seed (Repos + PMO). Initial useState often runs
// before the draft loads (length 0), so small fleets stay collapsed unless
// we seed once when the real count is known. Returns the index list to
// expand, or null when the caller must leave expansion alone.

export function fleetSeedIndexes(count, alreadySeeded) {
  if (alreadySeeded) return null;
  if (count <= 0) return null; // draft not loaded yet (or truly empty — wait)
  if (count <= 3) return Array.from({ length: count }, (_, i) => i);
  return []; // large fleet starts collapsed
}
