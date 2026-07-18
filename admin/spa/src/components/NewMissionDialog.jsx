import React, { useState } from "react";
import Button from "./Button.jsx";
import { Modal } from "./Modal.jsx";
import { Field, Input, Select, Textarea } from "./Field.jsx";
import { send } from "../api.js";

const PRIORITIES = [
  { value: "urgent", label: "Urgent" },
  { value: "high", label: "High" },
  { value: "medium", label: "Medium" },
  { value: "low", label: "Low" },
];

// Custom Modal-based form (PromptDialog only supports a single input, so it
// can't cover the title/description/priority/instance quartet).
export default function NewMissionDialog({ instances, onClose, onCreated }) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState("medium");
  const [instance, setInstance] = useState(
    instances && instances[0] ? instances[0].name : ""
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    const clean = title.trim();
    if (!clean || busy) return;
    setBusy(true);
    setError("");
    try {
      const body = {
        title: clean,
        description,
        priority,
        instance: instance || null,
      };
      const result = await send("POST", "/missions", body);
      onCreated(result);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal className="max-w-lg" onClose={busy ? undefined : onClose}>
      <h4 className="mb-1 text-base font-semibold tracking-tight">New mission</h4>
      <p className="mb-4 text-xs text-neutral-500 dark:text-neutral-400">
        Creates a backlog issue in Linear with the DEVCAKE label. ONBOARD triage
        picks it up on the next poll.
      </p>
      <div className="space-y-3">
        <Field label="Title" hint="Short summary — the Dev sees this verbatim.">
          <Input
            autoFocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && submit()}
            placeholder="e.g. Fix the login rate-limit off-by-one"
            aria-label="Mission title"
          />
        </Field>
        <Field label="Description" hint="Markdown, optional. Redacted before it's stored in Linear.">
          <Textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={4}
            placeholder="Context, acceptance criteria, links…"
            aria-label="Mission description"
          />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Priority">
            <Select
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
              aria-label="Priority"
            >
              {PRIORITIES.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </Select>
          </Field>
          <Field
            label="PMO instance"
            hint={
              instances && instances.length > 1
                ? "Which configured PMO owns this mission."
                : undefined
            }
          >
            <Select
              value={instance}
              onChange={(e) => setInstance(e.target.value)}
              aria-label="PMO instance"
              disabled={!instances || instances.length === 0}
            >
              {(!instances || instances.length === 0) && (
                <option value="">no instance configured</option>
              )}
              {instances &&
                instances.map((i) => (
                  <option key={i.name} value={i.name}>
                    {i.name} ({i.team_key})
                  </option>
                ))}
            </Select>
          </Field>
        </div>
      </div>
      {error && (
        <p className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300">
          {error}
        </p>
      )}
      <div className="mt-5 flex justify-end gap-2">
        <Button kind="ghost" disabled={busy} onClick={onClose}>
          Cancel
        </Button>
        <Button disabled={busy || !title.trim()} onClick={submit}>
          {busy ? "Creating…" : "Create mission"}
        </Button>
      </div>
    </Modal>
  );
}
