import { useEffect, useState } from "react";

// The name-rule constants live in draftErrors.js (dependency-free for the
// hermetic helper suite); re-exported here for the react-side consumers.
export { INSTANCE_NAME_RE, INSTANCE_NAME_RULE } from "./draftErrors.js";

// Next free default name ("repo2", "linear3"): skips names used by the draft
// OR still saved on the server. Without the server check, a card added right
// after deleting a saved "repo1" resurrects that exact name — and since
// stored tokens key on names, the new card would be born name-locked.
export function nextFreeName(base, draftRows, serverRows) {
  const used = new Set(
    [...(draftRows || []), ...(serverRows || [])].map((r) => r.name)
  );
  let i = (draftRows || []).length + 1;
  while (used.has(`${base}${i}`)) i++;
  return `${base}${i}`;
}

// Names lock once saved (stored secrets key on them) — but "name is in the
// server's set" alone is a trap: a card ADDED this session can carry a
// still-saved name (a deleted-but-unsaved server card, or a mid-typing
// collision with an existing one), and a disabled input can never un-disable
// itself. Track the names of cards added/renamed this session; those stay
// editable until a successful Save lands them on the server (the rebased
// draft row deep-equals the fresh server row), then they lock like the rest.
// `external` — an optional [names, setNames] pair owned by a component that
// OUTLIVES this hook's caller (audit D5 #12): the Config section components
// unmount on every section switch, so a section holding this state internally
// would lose in-progress "new card" tracking the moment the operator visits
// another section. The dispatcher (ConfigPage) stays mounted, so it owns the
// Set and threads it down. Falls back to internal state when unmanaged.
export function useNewNames(serverRows, draftRows, external) {
  const internal = useState(() => new Set());
  const [names, setNames] = external || internal;
  useEffect(() => {
    setNames((prev) => {
      if (prev.size === 0) return prev;
      const byName = new Map((serverRows || []).map((r) => [r.name, r]));
      const keep = [...prev].filter((n) => {
        const s = byName.get(n);
        // the TRACKED card is the last one carrying the name (mirrors the
        // pages' lock rule) — comparing the first would prune the tracked
        // name while a new card still duplicates an untouched saved card
        const d = [...(draftRows || [])].reverse().find((r) => r.name === n);
        return !(s && d && JSON.stringify(s) === JSON.stringify(d));
      });
      return keep.length === prev.size ? prev : new Set(keep);
    });
    // draftRows IS a dependency: a successful Save (rebase) and a Discard
    // (draft reset) both land the card identical to a server row — either
    // way it is now "the saved card" and must lock like one
  }, [serverRows, draftRows]);
  return {
    has: (n) => names.has(n),
    track: (n) => setNames((p) => new Set(p).add(n)),
    rename: (from, to) =>
      setNames((p) => {
        const n = new Set(p);
        n.delete(from);
        n.add(to);
        return n;
      }),
    untrack: (n) =>
      setNames((p) => {
        if (!p.has(n)) return p;
        const s = new Set(p);
        s.delete(n);
        return s;
      }),
  };
}
