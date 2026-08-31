import React, { useEffect, useMemo, useState } from "react";
import { get, send } from "../api.js";
import { connRef } from "../lib/connectionFields.js";
import Button from "./Button.jsx";
import { Field, Select } from "./Field.jsx";
import { Modal } from "./Modal.jsx";

// Slot-for-slot token copy across connection cards (user-scoped PATs are
// valid on every same-forge card, so one paste can feed the whole fleet).
// Values never touch the browser: the server reads the source's store and
// writes the targets' (POST /connections/copy-secrets). Saved cards only —
// secrets key on the server-side instance name.

export const TOKEN_COPY_ENTRY = {
  menuLabel: "Copy tokens between connections",
  desc: "Send one card's stored tokens to sibling cards — write, read-only, "
    + "and reviewer slots each land in their own slot.",
};

const FIELD_LABEL = {
  token: "Access token",
  token_ro: "Read-only token",
  reviewer_token: "Reviewer token",
  api_key: "API key",
};
const REPO_FIELDS = ["token", "token_ro", "reviewer_token"];

const keyOf = (scope, name) => `${scope}\0${name}`;

/** Fields the source card can send to one target: {targetField: sourceField}.
 *  Mirrors connections_service._copy_plan — forge-issues PMO cards take a
 *  same-forge repo's write token as their API key. */
function planFor(source, target) {
  if (source.scope === "repo") {
    if (target.scope === "repo") {
      if (target.forge !== source.forge) return null;
      return Object.fromEntries(REPO_FIELDS.map((f) => [f, f]));
    }
    if (target.system !== `${source.forge}_issues`) return null;
    return { api_key: "token" };
  }
  if (target.scope !== "pmo" || target.system !== source.system) return null;
  return { api_key: "api_key" };
}

export default function TokenCopyModal({ onClose, mode, repos, pmos }) {
  const cards = useMemo(() => [
    ...(repos || []).map((r) => ({ scope: "repo", name: r.name, forge: r.forge })),
    ...(pmos || []).map((p) => ({ scope: "pmo", name: p.name, system: p.system })),
  ], [repos, pmos]);
  const sources = cards.filter((c) => c.scope === mode);

  const [sourceKey, setSourceKey] = useState("");
  const [picked, setPicked] = useState(() => new Set());
  const [presence, setPresence] = useState(null);   // {ref: {present}}
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [results, setResults] = useState(null);

  useEffect(() => {
    const refs = cards.flatMap((c) => c.scope === "repo"
      ? REPO_FIELDS.map((f) => connRef("repo", c.name, f))
      : [connRef("pmo", c.name, "api_key")]);
    let dead = false;
    (async () => {
      const out = {};
      for (let i = 0; i < refs.length; i += 40) {
        const chunk = refs.slice(i, i + 40);
        try {
          const res = await get("/secrets-check?conn=" + chunk.join(","));
          Object.assign(out, res.conn || {});
        } catch { /* presence is advisory — the copy itself still validates */ }
      }
      if (!dead) setPresence(out);
    })();
    return () => { dead = true; };
  }, [cards]);

  const storedFields = (c) => (c.scope === "repo" ? REPO_FIELDS : ["api_key"])
    .filter((f) => presence?.[connRef(c.scope, c.name, f)]?.present);

  const source = sources.find((c) => keyOf(c.scope, c.name) === sourceKey);
  const eligible = useMemo(() => {
    if (!source) return [];
    return cards
      .filter((c) => !(c.scope === source.scope && c.name === source.name))
      .map((c) => ({ card: c, plan: planFor(source, c) }))
      .filter((e) => e.plan !== null)
      .map((e) => ({
        ...e,
        // what actually moves: mapped slots whose SOURCE side holds a value
        receives: Object.keys(e.plan).filter((tf) =>
          storedFields(source).includes(e.plan[tf])),
      }));
  }, [cards, source, presence]);

  const toggle = (k) => setPicked((prev) => {
    const next = new Set(prev);
    if (next.has(k)) next.delete(k);
    else next.add(k);
    return next;
  });

  const chosen = eligible.filter((e) => picked.has(keyOf(e.card.scope, e.card.name)));

  const doCopy = async () => {
    if (!source || !chosen.length || busy) return;
    setBusy(true);
    setErr("");
    try {
      const out = await send("POST", "/connections/copy-secrets", {
        source: { scope: source.scope, name: source.name },
        targets: chosen.map((e) => ({ scope: e.card.scope, name: e.card.name })),
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
              <span className="font-mono font-medium">{r.name}</span>
              <span className="text-neutral-600 dark:text-neutral-300">
                {r.copied.length
                  ? `✓ ${r.copied.map((f) => FIELD_LABEL[f] || f).join(", ")}`
                  : "nothing copied"}
                {r.skipped.length
                  ? ` · ${r.skipped.map((f) => FIELD_LABEL[f] || f).join(", ")} not stored on the source`
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
        into its own slot. Receiving cards&apos; existing values are replaced.
        Applies immediately — not part of the draft Save.
      </p>

      <Field label="Copy from" hint="Only saved cards with stored tokens can send.">
        <Select value={sourceKey} aria-label="Token copy source"
          onChange={(e) => { setSourceKey(e.target.value); setPicked(new Set()); }}>
          <option value="">Choose a card…</option>
          {sources.map((c) => {
            const stored = storedFields(c);
            return (
              <option key={keyOf(c.scope, c.name)} value={keyOf(c.scope, c.name)}
                disabled={presence !== null && !stored.length}>
                {c.name}
                {presence === null ? ""
                  : stored.length
                    ? ` — ${stored.map((f) => FIELD_LABEL[f]).join(", ")}`
                    : " — nothing stored"}
              </option>
            );
          })}
        </Select>
      </Field>

      {source && !eligible.length && (
        <p className="mt-3 text-sm text-neutral-500 dark:text-neutral-400">
          No other saved card can take this card&apos;s tokens
          {source.scope === "repo"
            ? ` (same-forge repositories and ${source.forge} issues boards only).`
            : " (same-system PMO cards only)."}
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
                : new Set(eligible.map((e) => keyOf(e.card.scope, e.card.name))))}>
              {chosen.length === eligible.length ? "Deselect all" : "Select all"}
            </button>
          </div>
          <div className="divide-y divide-neutral-100 rounded-md border border-neutral-200 px-3 py-1 dark:divide-neutral-800 dark:border-neutral-800">
            {eligible.map(({ card, receives }) => {
              const k = keyOf(card.scope, card.name);
              const id = `token-copy-${encodeURIComponent(k)}`;
              return (
                <div key={k} className="flex items-start gap-2.5 py-1.5">
                  <input id={id} type="checkbox" checked={picked.has(k)}
                    className="mt-0.5 h-4 w-4 accent-accent-600"
                    onChange={() => toggle(k)} />
                  <label htmlFor={id} className="min-w-0 cursor-pointer">
                    <span className="block font-mono text-sm font-medium">
                      {card.name}
                    </span>
                    <span className="block text-xs text-neutral-500 dark:text-neutral-400">
                      {card.scope === "pmo" && mode === "repo"
                        ? `${card.system} board — gets the Access token as its API key`
                        : receives.length
                          ? `gets ${receives.map((f) => FIELD_LABEL[f]).join(", ")}`
                          : "nothing to send yet"}
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
