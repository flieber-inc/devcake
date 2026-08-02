import React, { useEffect, useState } from "react";
import { get } from "../api.js";

// Shared identity + credential-readiness bits for the Dev Types roster and
// editor (split out of DevTypesSection 2026-08-02 with the roster-table
// conversion).

// Credential readiness for the DRAFTED harness: any stored env key or any
// present credential file counts. Same rule the dispatch checklist applies.
// The env-key probe is CACHED by credential-env set: 12 dev types share ≤3
// harness templates, so the roster fires ≤3 requests instead of one per row.
const credsCache = new Map();   // names-string → Promise<{VAR: present}>

export function clearCredsCache() {
  credsCache.clear();
}

function fetchEnvSet(names) {
  if (!credsCache.has(names)) {
    credsCache.set(
      names,
      get(`/secrets-check?harness=${encodeURIComponent(names)}`)
        .then((r) => Object.fromEntries(
          Object.entries(r.harness || {}).map(([k, v]) => [k, v.present])))
        .catch(() => { credsCache.delete(names); return {}; }),
    );
  }
  return credsCache.get(names);
}

export function useCredsReady(draftDt, serverDt, h) {
  const [envSet, setEnvSet] = useState({});
  useEffect(() => {
    setEnvSet({});
    const names = (h.credential_env || []).join(",");
    if (!names) return;
    let live = true;
    fetchEnvSet(names).then((set) => { if (live) setEnvSet(set); });
    return () => { live = false; };
  }, [draftDt.harness_template]);
  const filePresent = (sf) => (serverDt.secrets_present || []).includes(sf);
  return Object.values(envSet).some(Boolean) ||
    (h.credential_files || []).some((cf) => filePresent(cf.secret_file));
}

// Monogram avatar — DESIGN.md bans emoji/illustration flourishes, so the
// letterform in the accent tint carries the identity; the dot is credential
// readiness (green = ready, amber = none configured), not liveness.
const SIZES = {
  lg: { cls: "h-16 w-16 text-2xl", dot: "h-4 w-4" },
  sm: { cls: "h-10 w-10 text-base", dot: "h-3 w-3" },
  xs: { cls: "h-7 w-7 text-xs", dot: "h-2.5 w-2.5" },
};

export default function DevTypeAvatar({ name, ready, size = "lg" }) {
  const { cls, dot } = SIZES[size] || SIZES.lg;
  return (
    <span className="relative inline-flex">
      <span
        className={`inline-flex items-center justify-center rounded-full bg-accent-100 font-mono font-semibold uppercase text-accent-800 dark:bg-accent-950 dark:text-accent-200 ${cls}`}
        aria-hidden
      >
        {name.slice(0, 1)}
      </span>
      <span
        className={`absolute -bottom-0.5 -right-0.5 rounded-full border-2 border-surface-raised dark:border-surface-raised-dark ${dot} ${
          ready ? "bg-green-500" : "bg-amber-500"}`}
        title={ready ? "credentials ready" : "no credentials configured"}
      >
        <span className="sr-only">{ready ? "credentials ready" : "no credentials configured"}</span>
      </span>
    </span>
  );
}
