import React, { useEffect, useState } from "react";
import { send } from "../api.js";
import { Field, Input, Select } from "./Field.jsx";
import Button from "./Button.jsx";
import InstantZone from "./InstantZone.jsx";
import { Modal } from "./Modal.jsx";

const ROLES = [
  { id: "judge", label: "Judge", help: "ONBOARD, PLAN, REVIEW" },
  { id: "executor", label: "Executor", help: "EXECUTE" },
  { id: "steward", label: "Steward", help: "board-tending duties" },
];

function pickDefaultHarness(harnesses) {
  const keys = Object.keys(harnesses || {});
  if (keys.includes("claude-code")) return "claude-code";
  if (keys.includes("grok-build")) return "grok-build";
  return keys[0] || "";
}

function pickAltHarness(harnesses, primary) {
  const keys = Object.keys(harnesses || {});
  if (primary !== "grok-build" && keys.includes("grok-build")) return "grok-build";
  if (primary !== "claude-code" && keys.includes("claude-code")) return "claude-code";
  return keys.find((k) => k !== primary) || primary;
}

function blankRoles(harness) {
  return {
    judge: { harness_template: harness, model: "" },
    executor: { harness_template: harness, model: "" },
    steward: { harness_template: harness, model: "" },
  };
}

/** Empty-roster first-setup wizard (CAKE-164). Instant POST — not draft. */
export default function FirstSetupDialog({ harnesses, onClose, onCreated }) {
  const [roles, setRoles] = useState(() => blankRoles(pickDefaultHarness(harnesses)));
  const [sameHarness, setSameHarness] = useState(() => pickDefaultHarness(harnesses));
  const [sameModel, setSameModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    const h = pickDefaultHarness(harnesses);
    if (!h) return;
    setSameHarness((cur) => cur || h);
    setRoles((cur) => (cur.judge?.harness_template ? cur : blankRoles(h)));
  }, [harnesses]);

  const applySameForAll = () => {
    if (!sameHarness) return;
    setRoles({
      judge: { harness_template: sameHarness, model: sameModel },
      executor: { harness_template: sameHarness, model: sameModel },
      steward: { harness_template: sameHarness, model: sameModel },
    });
  };

  const applySplit = () => {
    const judgment = pickDefaultHarness(harnesses);
    const execution = pickAltHarness(harnesses, judgment);
    setRoles({
      judge: { harness_template: judgment, model: "" },
      executor: { harness_template: execution, model: "" },
      steward: { harness_template: judgment, model: "" },
    });
    setSameHarness(judgment);
    setSameModel("");
  };

  const setRole = (id, patch) => {
    setRoles((prev) => ({ ...prev, [id]: { ...prev[id], ...patch } }));
  };

  const submit = async () => {
    setBusy(true);
    setErr("");
    try {
      await send("POST", "/dev-types/first-setup", { roles });
      await onCreated();
      onClose();
    } catch (e) {
      setErr(String(e.message || e).replace(/^\d+ /, ""));
    } finally {
      setBusy(false);
    }
  };

  const harnessOptions = Object.keys(harnesses || {});
  const ready = ROLES.every((r) => roles[r.id]?.harness_template);

  return (
    <Modal onClose={busy ? undefined : onClose} className="max-w-xl p-6">
      <h4 className="mb-1 text-base font-semibold tracking-tight">
        First setup — create your first Devs
      </h4>
      <p className="mb-4 text-sm text-neutral-600 dark:text-neutral-300">
        Creates <span className="font-mono text-xs">judge</span>,{" "}
        <span className="font-mono text-xs">executor</span>, and{" "}
        <span className="font-mono text-xs">steward</span>, pins each CLI to a
        concrete version, and wires Mission Type assignments. Model may be
        left blank (= harness default). Existing non-empty rosters never see
        this wizard.
      </p>

      <InstantZone note="creates the roster immediately — does not wait for Save">
        <div className="space-y-3">
          <div className="rounded-md border border-neutral-200 bg-white/70 p-3 dark:border-neutral-700 dark:bg-neutral-900/40">
            <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
              Same for all
            </p>
            <div className="flex flex-wrap items-end gap-2">
              <div className="min-w-[10rem] flex-1">
                <Field label="Harness">
                  <Select value={sameHarness}
                    onChange={(e) => setSameHarness(e.target.value)}
                    aria-label="Same harness for all roles">
                    {harnessOptions.map((t) => (
                      <option key={t} value={t}>
                        {t}{harnesses[t]?.experimental ? " (experimental)" : ""}
                      </option>
                    ))}
                  </Select>
                </Field>
              </div>
              <div className="min-w-[10rem] flex-1">
                <Field label="Model (optional)"
                  hint={harnesses[sameHarness]?.default_model
                    ? `blank = ${harnesses[sameHarness].default_model}`
                    : "blank = harness default"}>
                  <Input value={sameModel}
                    onChange={(e) => setSameModel(e.target.value)}
                    placeholder="harness default"
                    aria-label="Same model for all roles" />
                </Field>
              </div>
              <Button kind="ghost" disabled={busy || !sameHarness}
                onClick={applySameForAll}>
                Prefill all
              </Button>
            </div>
            <div className="mt-2">
              <Button kind="ghost" disabled={busy || harnessOptions.length < 2}
                onClick={applySplit}
                title="Judge and Steward on one harness; Executor on another">
                Split judgment / execution harnesses
              </Button>
            </div>
          </div>

          <div className="divide-y divide-neutral-100 dark:divide-neutral-800">
            {ROLES.map((r) => {
              const row = roles[r.id] || { harness_template: "", model: "" };
              const h = harnesses[row.harness_template] || {};
              return (
                <div key={r.id} className="flex flex-wrap items-end gap-2 py-3">
                  <div className="min-w-[6.5rem]">
                    <p className="text-sm font-semibold">{r.label}</p>
                    <p className="font-mono text-[10px] text-neutral-500">{r.id}</p>
                    <p className="text-[11px] text-neutral-500 dark:text-neutral-400">{r.help}</p>
                  </div>
                  <div className="min-w-[9rem] flex-1">
                    <Field label="Harness">
                      <Select value={row.harness_template}
                        aria-label={`${r.label} harness`}
                        onChange={(e) => setRole(r.id, {
                          harness_template: e.target.value,
                        })}>
                        {harnessOptions.map((t) => (
                          <option key={t} value={t}>
                            {t}{harnesses[t]?.experimental ? " (experimental)" : ""}
                          </option>
                        ))}
                      </Select>
                    </Field>
                  </div>
                  <div className="min-w-[9rem] flex-1">
                    <Field label="Model"
                      hint={h.default_model ? `blank = ${h.default_model}` : "optional"}>
                      <Input value={row.model}
                        aria-label={`${r.label} model`}
                        onChange={(e) => setRole(r.id, { model: e.target.value })}
                        placeholder="harness default" />
                    </Field>
                  </div>
                </div>
              );
            })}
          </div>

          {err && (
            <p className="text-sm text-red-600 dark:text-red-400">✗ {err}</p>
          )}
          <div className="flex justify-end gap-2">
            <Button kind="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
            <Button disabled={busy || !ready} onClick={submit}>
              {busy ? "Creating…" : "Create Executor, Judge & Steward"}
            </Button>
          </div>
        </div>
      </InstantZone>
    </Modal>
  );
}
