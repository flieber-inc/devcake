import React, { useEffect, useMemo, useState } from "react";
import { get, send } from "../api.js";
import {
  CONNECTION_FIELD_LABELS, CONNECTION_FIELDS, connRef,
} from "../lib/connectionFields.js";
import Button from "./Button.jsx";
import { Field, Select } from "./Field.jsx";
import { Modal } from "./Modal.jsx";

// Slot-for-slot token copy across connection cards (user-scoped PATs are
// valid on every same-forge SAME-HOST card, so one paste can feed the
// fleet). Values never touch the browser: the server reads the source's
// store and writes the targets' (POST /connections/copy-secrets). The
// SERVER also decides eligibility — picking a source fires a dry_run and
// the target list renders from its rows, so no compatibility table is
// mirrored client-side to drift. Saved cards only (secrets key on the
// server-side instance name); the host pages pass lists already filtered
// by their saved-name predicate.

export const TOKEN_COPY_ENTRY = {
  menuLabel: "Copy tokens between connections",
  desc: "Send one card's stored tokens to sibling cards — write, read-only, "
    + "and reviewer slots each land in their own slot.",
};

const keyOf = (scope, name) => `${scope}\0${name}`;
const label = (f) => CONNECTION_FIELD_LABELS[f] || f;

export default function TokenCopyModal({ onClose, mode, repos, pmos }) {
  const cards = useMemo(() => [
    ...(repos || []).map((r) => ({ scope: "repo", name: r.name })),
    ...(pmos || []).map((p) => ({ scope: "pmo", name: p.name })),
  ], [repos, pmos]);
  const sources = cards.filter((c) => c.scope === mode);

  const [sourceKey, setSourceKey] = useState("");
  const [picked, setPicked] = useState(() => new Set());
  // presence decorates the SOURCE options only; a failed check keeps it
  // null = unknown (advisory — never allowed to brick the picker)
  const [presence, setPresence] = useState(null);
  const [preview, setPreview] = useState(null);   // dry-run rows for source
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [results, setResults] = useState(null);

  useEffect(() => {
    const refs = sources.flatMap((c) => c.scope === "repo"
      ? CONNECTION_FIELDS.repo.map((f) => connRef("repo", c.name, f))
      : [connRef("pmo", c.name, "api_key")]);
    let dead = false;
    (async () => {
      const chunks = [];
      for (let i = 0; i < refs.length; i += 40) chunks.push(refs.slice(i, i + 40));
      const parts = await Promise.all(chunks.map((chunk) =>
        get("/secrets-check?conn=" + encodeURIComponent(chunk.join(",")))
          .then((r) => r.conn || {})
          .catch(() => null)));
      if (dead) return;
      if (parts.every((p) => p === null)) return;   // all failed: stay unknown
      setPresence(Object.assign({}, ...parts.filter(Boolean)));
    })();
    return () => { dead = true; };
    // sources derives from the cards prop — refetch only when it changes
  }, [cards, mode]);   // eslint-disable-line react-hooks/exhaustive-deps

  const storedFields = (c) => (c.scope === "repo"
    ? CONNECTION_FIELDS.repo : ["api_key"])
    .filter((f) => presence?.[connRef(c.scope, c.name, f)]?.present);

  const source = sources.find((c) => keyOf(c.scope, c.name) === sourceKey);

  const chooseSource = async (key) => {
    setSourceKey(key);
    setPicked(new Set());
    setPreview(null);
    setErr("");
    const chosen = sources.find((c) => keyOf(c.scope, c.name) === key);
    if (!chosen) return;
    setBusy(true);
    try {
      const out = await send("POST", "/connections/copy-secrets", {
        dry_run: true,
        source: { scope: chosen.scope, name: chosen.name },
        targets: cards
          .filter((c) => !(c.scope === chosen.scope && c.name === chosen.name))
          .map((c) => ({ scope: c.scope, name: c.name })),
      });
      setPreview(out.targets || []);
    } catch (e) {
      setErr(String(e.message || e).replace(/^\d+ /, ""));
    } finally {
      setBusy(false);
    }
  };

  const eligible = (preview || []).filter(
    (r) => r.eligible && r.receives.length);
  const chosen = eligible.filter((r) => picked.has(keyOf(r.scope, r.name)));

  const toggle = (k) => setPicked((prev) => {
    const next = new Set(prev);
    if (next.has(k)) next.delete(k);
    else next.add(k);
    return next;
  });

  const doCopy = async () => {
    if (!source || !chosen.length || busy) return;
    setBusy(true);
    setErr("");
    try {
      const out = await send("POST", "/connections/copy-secrets", {
        source: { scope: source.scope, name: source.name },
        targets: chosen.map((r) => ({ scope: r.scope, name: r.name })),
      });
      setResults(out.results || []);
    } catch (e) {
      setErr(String(e.message || e).replace(/^\d+ /, ""));
    } finally {
      setBusy(false);
    }
  };

  if (results) {
    return (
      <Modal className="max-w-xl" onClose={onClose}>
        <h4 className="mb-1 text-base font-semibold tracking-tight">Tokens copied</h4>
        <ul className="mb-4 space-y-1 text-sm">
          {results.map((r) => (
            <li key={keyOf(r.scope, r.name)} className="flex flex-wrap gap-x-2">
              <span className="font-mono font-medium">
                {r.scope === "pmo" ? "board " : "repo "}{r.name}
              </span>
              <span className="text-neutral-600 dark:text-neutral-300">
                {r.copied.length
                  ? `✓ ${r.copied.map(label).join(", ")}`
                  : "nothing copied"}
                {r.skipped.length
                  ? ` · ${r.skipped.map(label).join(", ")} not stored on the source`
                  : ""}
              </span>
            </li>
          ))}
        </ul>
        <div className="flex justify-end">
          <Button onClick={onClose}>Done</Button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal className="max-w-xl" onClose={busy ? undefined : onClose}>
      <h4 className="mb-1 text-base font-semibold tracking-tight">
        Copy tokens between connections
      </h4>
      <p className="mb-4 text-sm text-neutral-500 dark:text-neutral-400">
        Sends the source card&apos;s stored tokens to the cards you pick, each
        into its own slot — same forge, same host only. Receiving cards&apos;
        existing values are replaced. Applies immediately — not part of the
        draft Save.
      </p>

      <Field label="Copy from"
        hint="Only saved cards with stored tokens can send.">
        <Select value={sourceKey} aria-label="Token copy source"
          disabled={busy}
          onChange={(e) => chooseSource(e.target.value)}>
          <option value="">Choose a card…</option>
          {sources.map((c) => {
            const stored = presence === null ? null : storedFields(c);
            return (
              <option key={keyOf(c.scope, c.name)} value={keyOf(c.scope, c.name)}
                disabled={stored !== null && !stored.length}>
                {c.name}
                {stored === null ? ""
                  : stored.length
                    ? ` — ${stored.map(label).join(", ")}`
                    : " — nothing stored"}
              </option>
            );
          })}
        </Select>
      </Field>

      {source && preview && !eligible.length && (
        <p className="mt-3 text-sm text-neutral-500 dark:text-neutral-400">
          No other saved card can take this card&apos;s tokens right now
          (same forge and host only, and the source must hold the slot the
          receiver needs).
        </p>
      )}

      {source && eligible.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
              Copy to
            </span>
            <button type="button"
              className="text-xs text-accent-700 underline-offset-2 hover:underline dark:text-accent-400"
              onClick={() => setPicked(chosen.length === eligible.length
                ? new Set()
                : new Set(eligible.map((r) => keyOf(r.scope, r.name))))}>
              {chosen.length === eligible.length ? "Deselect all" : "Select all"}
            </button>
          </div>
          <div className="divide-y divide-neutral-100 rounded-md border border-neutral-200 px-3 py-1 dark:divide-neutral-800 dark:border-neutral-800">
            {eligible.map((r) => {
              const k = keyOf(r.scope, r.name);
              const id = `token-copy-${encodeURIComponent(k)}`;
              return (
                <div key={k} className="flex items-start gap-2.5 py-1.5">
                  <input id={id} type="checkbox" checked={picked.has(k)}
                    className="mt-0.5 h-4 w-4 accent-accent-600"
                    onChange={() => toggle(k)} />
                  <label htmlFor={id} className="min-w-0 cursor-pointer">
                    <span className="block font-mono text-sm font-medium">
                      {r.name}
                    </span>
                    <span className="block text-xs text-neutral-500 dark:text-neutral-400">
                      {r.scope === "pmo" && source.scope === "repo"
                        ? `issues board — gets the ${label("token")} as its ${label("api_key")}`
                        : `gets ${r.receives.map(label).join(", ")}`}
                    </span>
                  </label>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {err && (
        <p className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300">
          ✗ {err}
        </p>
      )}

      <div className="mt-5 flex justify-end gap-2">
        <Button kind="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
        <Button disabled={busy || !chosen.length} onClick={doCopy}>
          {chosen.length
            ? `Copy to ${chosen.length} card${chosen.length === 1 ? "" : "s"}`
            : "Copy"}
        </Button>
      </div>
    </Modal>
  );
}
