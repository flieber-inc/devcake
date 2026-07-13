// Timestamp helpers for run listings. API timestamps are ISO strings (UTC).

export function fullTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

export function relTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  if (s < 0) return "now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const days = Math.floor(h / 24);
  return days < 7 ? `${days}d ago` : d.toLocaleDateString();
}

export function duration(startIso, endIso) {
  if (!startIso) return "—";
  const start = new Date(startIso);
  if (isNaN(start)) return "—";
  const end = endIso ? new Date(endIso) : new Date();
  let s = Math.max(0, Math.round((end - start) / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  s = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}
