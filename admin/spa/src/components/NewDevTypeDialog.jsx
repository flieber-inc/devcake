import React, { useState } from "react";
import { send } from "../api.js";
import { Field, Input, Select } from "./Field.jsx";
import Button from "./Button.jsx";
import { Modal } from "./Modal.jsx";

// "New Dev Type" dialog (split out of DevTypesSection 2026-08-02): name +
// harness picked up front — the old flow created "new-dev-###" instantly and
// made the operator rename it after. On success the editor opens on the new
// Dev Type so setup continues there.
export default function NewDevTypeDialog({ harnesses, onClose, onCreated }) {
  const [name, setName] = useState("");
  const [harness, setHarness] = useState(Object.keys(harnesses)[0] || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const create = async () => {
    setBusy(true); setErr("");
    try {
      await send("POST", "/dev-types", {
        name: name.trim(), harness_template: harness,
        identifying_prompt: "", max_concurrency: 1,
      });
      await onCreated(name.trim()); onClose();
    } catch (e) { setErr(String(e.message || e).replace(/^\d+ /, "")); }
    finally { setBusy(false); }
  };
  return (
    <Modal onClose={busy ? undefined : onClose}>
      <h4 className="mb-3 text-base font-semibold tracking-tight">New Dev Type</h4>
      <div className="space-y-3">
        <Field label="Name" hint="lowercase letters/digits/dashes — e.g. judgment">
          <Input value={name} onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && name.trim() && !busy && create()} />
        </Field>
        <Field label="Harness template"
          help="Which coding agent this Dev runs. The Docker image and credential requirements follow it — both can be changed later. Experimental ids have not passed a live operator battery.">
          <Select value={harness} onChange={(e) => setHarness(e.target.value)}>
            {Object.keys(harnesses).map((t) => (
              <option key={t} value={t}>
                {t}{harnesses[t]?.experimental ? " (experimental)" : ""}
              </option>
            ))}
          </Select>
        </Field>
        {harnesses[harness]?.experimental && (
          <p className="text-xs text-amber-600 dark:text-amber-400">
            Experimental — in-tree so it can be dispatched, but it has not
            passed a live operator battery.
          </p>
        )}
        {err && <p className="text-sm text-red-600 dark:text-red-400">✗ {err}</p>}
        <div className="flex justify-end gap-2">
          <Button kind="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
          <Button disabled={busy || !name.trim() || !harness} onClick={create}>
            {busy ? "Creating…" : "Create Dev Type"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
