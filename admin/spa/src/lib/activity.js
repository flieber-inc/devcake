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

/**
 * Summarise the payload for the bar. `nowMs` is the client clock; elapsed
 * values come from the server so clock skew never invents a stall.
 * state: "idle" | "busy" | "stalled" | "unknown"
 */
export function summarizeActivity(payload, nowMs = Date.now()) {
  if (!payload || typeof payload !== "object") {
    return { state: "unknown", line: "activity unavailable", items: [], skips: [] };
  }
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
  if (items.length === 0) {
    let idle = "";
    if (payload.idle_since) {
      const secs = (nowMs - Date.parse(payload.idle_since)) / 1000;
      idle = Number.isFinite(secs) ? ` for ${formatElapsed(secs)}` : "";
    }
    const last = (payload.recent || [])[0];
    const lastText = last ? ` · last: ${phaseLabel(last)} in ${formatElapsed(last.elapsed_s)}` : "";
    const skipText = skips.length ? ` · ${skips.length} board${skips.length > 1 ? "s" : ""} waiting` : "";
    return { state: skips.length ? "waiting" : "idle", line: `idle${idle}${lastText}${skipText}`, items, skips };
  }
  const stalled = items.some((i) => i.overdue);
  const head = items.slice(0, 3).map((i) => i.label).join(" · ");
  const more = items.length > 3 ? ` · +${items.length - 3} more` : "";
  const n = items.length;
  return {
    state: stalled ? "stalled" : "busy",
    line: `${n} in flight — ${head}${more}${stalled ? " · one phase is overdue" : ""}`,
    items, skips,
  };
}
