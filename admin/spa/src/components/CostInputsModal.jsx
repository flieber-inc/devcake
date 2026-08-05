import React, { useEffect, useState } from "react";
import { Trash2 } from "lucide-react";
import { get, send } from "../api.js";
import { Modal } from "./Modal.jsx";
import Button from "./Button.jsx";
import { Field, Input } from "./Field.jsx";
import InstantZone from "./InstantZone.jsx";

// Cost Inputs (ADR-0021): the operator rate card behind every estimated
// cost, edited from the Runs page it affects. Instant regime — Save PUTs
// {cost_inputs} straight through /config (validation + rollback there) and
// the Runs table repriced on its next poll; `cfg.cost_inputs` sits in the
// draft's IGNORED list so an open Configuration draft can't clobber this.
const RATE_COLS = [
  ["input_per_mtok", "Input"],
  ["cache_read_per_mtok", "Cache read"],
  ["cache_write_per_mtok", "Cache write"],
  ["output_per_mtok", "Output"],
];

export default function CostInputsModal({ onClose, onSaved }) {
  const [rates, setRates] = useState(null);
  const [override, setOverride] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    get("/config")
      .then((c) => {
        const ci = c.cost_inputs || { rates: [], override_native: false };
        setRates((ci.rates || []).map((r) => ({ ...r })));
        setOverride(!!ci.override_native);
      })
      .catch((e) => setErr(String(e.message || e)));
  }, []);

  const prefixes = (rates || []).map((r) => (r.model_prefix || "").trim());
  const invalid =
    prefixes.some((p) => !p) ||
    new Set(prefixes).size !== prefixes.length ||
    (rates || []).some((r) =>
      RATE_COLS.some(([k]) => r[k] !== "" && Number(r[k]) < 0));

  const setRow = (i, key, value) =>
    setRates(rates.map((r, j) => (j === i ? { ...r, [key]: value } : r)));

  const save = async () => {
    setBusy(true);
    setErr("");
    try {
      await send("PUT", "/config", {
        cost_inputs: {
          rates: rates.map((r) => ({
            model_prefix: r.model_prefix.trim(),
            ...Object.fromEntries(
              RATE_COLS.map(([k]) => [k, Number(r[k]) || 0])),
          })),
          override_native: override,
        },
      });
      onSaved?.();
      onClose();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal onClose={busy ? undefined : onClose} className="max-w-3xl">
      <h4 className="mb-1 text-base font-semibold tracking-tight">Cost inputs</h4>
      <p className="mb-3 text-sm text-neutral-500 dark:text-neutral-400">
        USD per 1M tokens, matched by the longest model-name prefix. These
        rates power every <em>estimated</em> cost — runs whose model matches
        no row are never priced, and the harness&apos;s own reported cost is
        never rewritten.
      </p>
      {rates === null && !err && (
        <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading…</p>
      )}
      {rates !== null && (
        <InstantZone note="Save applies immediately — the Runs table reprices on its next refresh">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
                <tr>
                  <th className="py-1 pr-2">Model prefix</th>
                  {RATE_COLS.map(([k, label]) => (
                    <th key={k} className="pr-2">{label} $/1M</th>
                  ))}
                  <th />
                </tr>
              </thead>
              <tbody>
                {rates.length === 0 && (
                  <tr><td colSpan={6} className="py-3 text-center text-sm text-neutral-500 dark:text-neutral-400">
                    No rate rows — nothing gets an estimated cost.
                  </td></tr>
                )}
                {rates.map((r, i) => (
                  <tr key={i} className="border-t border-neutral-100 dark:border-neutral-800">
                    <td className="py-1.5 pr-2">
                      <Input value={r.model_prefix}
                        aria-label={`Model prefix for row ${i + 1}`}
                        placeholder="e.g. grok-4.5"
                        onChange={(e) => setRow(i, "model_prefix", e.target.value)} />
                    </td>
                    {RATE_COLS.map(([k]) => (
                      <td key={k} className="pr-2">
                        <Input type="number" min="0" step="0.01" className="w-24"
                          aria-label={`${k} for row ${i + 1}`}
                          value={r[k]}
                          onChange={(e) => setRow(i, k, e.target.value)} />
                      </td>
                    ))}
                    <td className="text-right">
                      <Button kind="danger-ghost" icon={Trash2}
                        aria-label={`Remove rate row ${i + 1}`}
                        onClick={() => setRates(rates.filter((_, j) => j !== i))} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Button kind="ghost" onClick={() =>
            setRates([...rates, { model_prefix: "", input_per_mtok: "",
                                  cache_read_per_mtok: "", cache_write_per_mtok: "",
                                  output_per_mtok: "" }])}>
            + Add model
          </Button>
          <Field label="">
            <label className="flex items-start gap-2 text-sm">
              <input type="checkbox" className="mt-0.5" checked={override}
                onChange={(e) => setOverride(e.target.checked)} />
              <span>
                Use these rates for displayed cost even when the harness
                reported its own.
                <span className="block text-xs text-neutral-500 dark:text-neutral-400">
                  Only affects models with a matching row — to override
                  Claude&apos;s native cost, add a <code className="font-mono text-[11px]">claude-</code> row.
                  Unmatched models always fall back to the harness figure.
                </span>
              </span>
            </label>
          </Field>
        </InstantZone>
      )}
      {err && <p className="mt-2 text-sm text-red-600 dark:text-red-400">⚠ {err}</p>}
      <div className="mt-4 flex justify-end gap-2">
        <Button kind="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
        <Button disabled={busy || rates === null || invalid} onClick={save}>
          {busy ? "Saving…" : "Save rates"}
        </Button>
      </div>
    </Modal>
  );
}
