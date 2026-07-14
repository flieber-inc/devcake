import React, { useEffect, useState } from "react";
import { get } from "../api.js";
import { getRegistry } from "../lib/registry.js";

const inputCls =
  "w-full rounded-md border border-neutral-300 bg-white px-2.5 py-1.5 text-sm " +
  "transition focus:outline-none focus:ring-2 focus:ring-accent-500/50 focus:border-accent-400 " +
  "dark:border-neutral-700 dark:bg-neutral-950";

export function Input({ className = "", ...props }) {
  return <input className={`${inputCls} ${className}`} {...props} />;
}

export function Select({ className = "", children, ...props }) {
  return <select className={`${inputCls} ${className}`} {...props}>{children}</select>;
}

export function Textarea({ className = "", ...props }) {
  return <textarea className={`${inputCls} ${className}`} {...props} />;
}

export function Help({ text }) {
  return (
    <span className="group relative ml-1 inline-block align-middle">
      <span className="flex h-4 w-4 cursor-help items-center justify-center rounded-full bg-neutral-200 text-[10px] font-semibold text-neutral-500 dark:bg-neutral-700 dark:text-neutral-300">
        ?
      </span>
      <span className="pointer-events-none invisible absolute left-1/2 top-full z-40 mt-1.5 w-64 -translate-x-1/2 rounded-md bg-neutral-900 p-2 text-xs font-normal leading-relaxed text-white opacity-0 shadow-lg transition group-hover:visible group-hover:opacity-100 dark:bg-neutral-700">
        {text}
      </span>
    </span>
  );
}

export function Field({ label, hint, help, children }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium">
        {label}
        {help && <Help text={help} />}
      </span>
      {children}
      {hint && <span className="mt-1 block text-xs text-neutral-400">{hint}</span>}
    </label>
  );
}

// ── env-var-name fields (the token_env incident, 2026-07-11) ────────────────
// These fields want the NAME of an env var; pasting the secret itself silently
// yields an empty token at runtime. Warn on secret-shaped values and show
// live set/unset status from the app's environment.

const ENV_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
// harness key shapes stay static; PMO/forge token prefixes come from the
// adapter registry so a new adapter's secrets are guarded without SPA edits
const GENERIC_SECRET_PREFIXES = ["sk-", "xai-"];

function secretShapeRe() {
  const prefixes = [...GENERIC_SECRET_PREFIXES,
                    ...getRegistry().secret_shape_prefixes];
  const escaped = prefixes.map((p) => p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  return new RegExp(`^(${escaped.join("|")})`);
}

export function envProblem(value) {
  if (!value) return null;
  if (secretShapeRe().test(value) || value.length > 40)
    return "This looks like a secret VALUE. This field wants the NAME of an " +
           "environment variable (e.g. GITHUB_TOKEN); the secret itself goes " +
           "in DevCake's .env.";
  if (!ENV_NAME_RE.test(value))
    return "Not a valid env var name (letters, digits and underscores only).";
  return null;
}

// `fallback`: the derived default env-var name (e.g. the forge descriptor's
// token_env_default) used when the field is left empty — shown as the input
// placeholder, and the ✓/✗ probe checks it so "empty = derived" stays honest.
export function EnvVarField({ label, help, hint, value, onChange, fallback }) {
  const effective = value || fallback || "";
  const [isSet, setIsSet] = useState(null); // null = unknown
  const problem = envProblem(value);
  useEffect(() => {
    setIsSet(null);
    if (!effective || envProblem(value)) return;
    const t = setTimeout(
      () => get(`/env-check?names=${encodeURIComponent(effective)}`)
        .then((r) => setIsSet(!!r[effective]))
        .catch(() => setIsSet(null)),
      400
    );
    return () => clearTimeout(t);
  }, [effective]);
  const suffix = !value && fallback ? ` (default: ${fallback})` : "";
  return (
    <Field label={label} help={help} hint={hint}>
      <Input value={value || ""} onChange={onChange} placeholder={fallback || ""} />
      {problem && <span className="mt-1 block text-xs text-red-600">⚠ {problem}</span>}
      {!problem && effective && isSet === false && (
        <span className="mt-1 block text-xs text-amber-600 dark:text-amber-400">
          ✗ {effective} is not set in DevCake's environment — add it to .env and restart
        </span>
      )}
      {!problem && effective && isSet === true && (
        <span className="mt-1 block text-xs text-green-700 dark:text-green-400">✓ set{suffix}</span>
      )}
    </Field>
  );
}
