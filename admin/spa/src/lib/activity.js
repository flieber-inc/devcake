// The status bar's reading of GET /api/v1/activity (docs/11 §0): what the
// app is doing right now, or how long it has been idle. Pure — covered by
// tests/activity_helpers.mjs.

const KIND_LABEL = {
  "poll.cycle": (i) => `poll ${i.subject}`,
  "poll.instance": (i) => `polling ${i.subject}`,
  "mirror.sync": (i) => `syncing mirrors${progress(i)}`,
  "forge.sweep": (i) => `probing repositories${progress(i)}`,
  "mission.dispatch": (i) => `dispatching ${i.subject}`,
  "run.finalize": (i) => `finalizing ${i.subject}`,
  "steward.launch": (i) => `launching the steward (${i.subject})`,
  "pmo.budget.wait": (i) => `waiting for tracker quota${i.detail?.wait_s != null ? ` (${Math.round(i.detail.wait_s)} s)` : ""}`,
  "config.apply": (i) => `${i.subject}${i.detail?.state ? ` — ${i.detail.state}` : ""}`,
};

function progress(i) {
  const d = i.detail || {};
  if (typeof d.done === "number" && typeof d.total === "number") return ` ${d.done}/${d.total}`;
  return "";
}

export function formatElapsed(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} m ${s % 60} s`;
  const h = Math.floor(m / 60);
  return `${h} h ${m % 60} m`;
}

export function phaseLabel(item) {
  const f = KIND_LABEL[item.kind];
  return f ? f(item) : `${item.kind}${item.subject ? ` ${item.subject}` : ""}`;
}

const DEAD_LOOP_FACTOR = 2;   // no cycle for > interval × factor, nothing in flight

/**
 * Summarise the payload for the bar. Every duration is measured against
 * the SERVER's clock (`payload.now`, falling back to `nowMs`), so a viewer's
 * clock skew never invents a stall or hides one.
 * state: "idle" | "waiting" | "busy" | "stalled" | "unknown"
 */
export function summarizeActivity(payload, nowMs = Date.now()) {
  if (!payload || typeof payload !== "object") {
    return { state: "unknown", line: "activity unavailable", items: [], skips: [] };
  }
  const serverNow = Date.parse(payload.now);
  const ref = Number.isFinite(serverNow) ? serverNow : nowMs;
  const items = (payload.items || []).map((i) => ({
    label: phaseLabel(i),
    elapsed: formatElapsed(i.elapsed_s),
    overdue: !!i.overdue,
    kind: i.kind,
  }));
  const skips = Object.entries(payload.poll_skips || {}).map(([inst, s]) => ({
    label: `${inst}: poll segment skipped — ${s.reason || "transient trouble"}`
      + (s.retry_after_s != null ? ` (retry after ${Math.round(s.retry_after_s)} s)` : ""),
    at: s.at,
  }));
  const skipText = skips.length ? ` · ${skips.length} board${skips.length > 1 ? "s" : ""} waiting` : "";
  if (items.length === 0) {
    // the one frozen state the registry cannot see: a dead poll loop
    const interval = Number(payload.poll_interval_s);
    const lastPoll = Date.parse(payload.last_poll_at);
    if (interval > 0 && Number.isFinite(lastPoll)) {
      const since = (ref - lastPoll) / 1000;
      if (since > interval * DEAD_LOOP_FACTOR) {
        return {
          state: "stalled",
          line: `no poll cycle finished for ${formatElapsed(since)} (interval ${formatElapsed(interval)}) and nothing is in flight — the poll loop may be wedged${skipText}`,
          items, skips,
        };
      }
    }
    let idle = "";
    if (payload.idle_since) {
      const secs = (ref - Date.parse(payload.idle_since)) / 1000;
      idle = Number.isFinite(secs) ? ` for ${formatElapsed(secs)}` : "";
    }
    const last = (payload.recent || [])[0];
    const lastText = last ? ` · last: ${phaseLabel(last)} in ${formatElapsed(last.elapsed_s)}` : "";
    return { state: skips.length ? "waiting" : "idle", line: `idle${idle}${lastText}${skipText}`, items, skips };
  }
  const stalled = items.some((i) => i.overdue);
  const head = items.slice(0, 3).map((i) => i.label).join(" · ");
  const more = items.length > 3 ? ` · +${items.length - 3} more` : "";
  const n = items.length;
  return {
    state: stalled ? "stalled" : "busy",
    line: `${n} in flight — ${head}${more}${stalled ? " · one phase is overdue" : ""}${skipText}`,
    items, skips,
  };
}
