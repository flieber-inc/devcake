// Stale-response guard (2026-08-12 audit): several loaders re-fire on
// changing deps (RunsPage's eight filter deps) or from many call sites with
// no in-flight guard (ConfigDraftContext.reload). Without ordering, an
// in-flight response for an OLD query resolving AFTER a newer one commits
// wrong-query data — RunsPage self-corrects at the next 10s tick, but a
// config rebase rolls the server baseline BACKWARD and the phantom dirt
// persists (config does not poll). No AbortController rewrite needed: a
// monotonic sequence per surface, and a resolution whose sequence is not
// the latest is dropped.
//
// Usage:
//   const seq = makeReqSeq();
//   const load = async () => {
//     const mine = seq.next();
//     const data = await fetch(...);
//     if (!seq.isLatest(mine)) return;   // a newer load already committed
//     commit(data);
//   };
export function makeReqSeq() {
  let latest = 0;
  return {
    next() {
      latest += 1;
      return latest;
    },
    isLatest(token) {
      return token === latest;
    },
  };
}
