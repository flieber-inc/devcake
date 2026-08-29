// Client-side filtered list window — page a name list without a silent
// hard cap (CAKE-145). Callers reset pageIndex to 0 when the query changes;
// this util still clamps a stale high pageIndex to the last page.

/**
 * @param {string[]} names ordered names
 * @param {string} query case-insensitive substring; empty → all names
 * @param {number} pageSize
 * @param {number} pageIndex 0-based
 * @returns {{ matched: string[], pageNames: string[], pageIndex: number, pageCount: number, totalMatched: number }}
 */
export function listWindow(names, query, pageSize, pageIndex) {
  const q = (query || "").trim().toLowerCase();
  const matched = q
    ? names.filter((n) => n.toLowerCase().includes(q))
    : names.slice();
  const totalMatched = matched.length;
  const pageCount = totalMatched === 0 ? 0 : Math.ceil(totalMatched / pageSize);
  let page = Number(pageIndex) || 0;
  if (pageCount === 0) page = 0;
  else if (page < 0) page = 0;
  else if (page >= pageCount) page = pageCount - 1;
  const start = page * pageSize;
  const pageNames = matched.slice(start, start + pageSize);
  return { matched, pageNames, pageIndex: page, pageCount, totalMatched };
}

/** 0-based page that holds absolute list index `i` at the given pageSize. */
export function pageForIndex(i, pageSize) {
  if (pageSize <= 0) return 0;
  return Math.floor(Math.max(0, i) / pageSize);
}
