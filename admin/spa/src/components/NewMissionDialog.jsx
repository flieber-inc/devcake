import React, { useEffect, useRef, useState } from "react";
import Button from "./Button.jsx";
import { Modal, ConfirmDialog } from "./Modal.jsx";
import { Field, Input, Select, Textarea } from "./Field.jsx";
import { get, send } from "../api.js";
import { fileToB64 } from "../lib/files.js";

const PRIORITIES = [
  { value: "urgent", label: "Urgent" },
  { value: "high", label: "High" },
  { value: "medium", label: "Medium" },
  { value: "low", label: "Low" },
];
const MAX_ATTACHMENTS = 10;

// ADR-0030 mission composer: transcribes an operator-originated mission onto
// the chosen PMO board — write-through to POST /api/v1/missions, no local
// record (the PMO stays the source of truth). Resurrects the PR-#14-deleted
// dialog with the pieces its deletion rationale demanded: attachments in v1,
// per-adapter field honesty (priority hidden where the adapter ignores it),
// an adoption toggle that owns the opt-in truth, and a ConfirmDialog whose
// copy is assembled from the ACTUAL request (founder safety requirement).
export default function NewMissionDialog({ adoptionMode, onClose, onCreated }) {
  const [pmos, setPmos] = useState(null);          // configured instances
  const [registry, setRegistry] = useState({ pmo_systems: [] });
  const [instance, setInstance] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState("medium");
  const [adopt, setAdopt] = useState(true);
  const [files, setFiles] = useState([]);          // File objects
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fileInput = useRef(null);

  useEffect(() => {
    // /missions carries instance NAMES only — system (and thus priority
    // support) needs the config list + the adapter registry
    Promise.all([get("/config"), get("/connections/registry")])
      .then(([cfg, reg]) => {
        const configured = (cfg.pmos || []).filter((p) => p.team_key);
        setPmos(configured);
        setRegistry(reg);
        if (configured.length) setInstance(configured[0].name);
      })
      .catch((e) => setError(String(e.message || e)));
  }, []);

  const inst = (pmos || []).find((p) => p.name === instance);
  const sysMeta = registry.pmo_systems.find((s) => s.id === inst?.system);
  const supportsPriority = sysMeta ? sysMeta.supports_priority !== false : true;
  const showAdopt = adoptionMode === "opt_in";

  const pickFiles = (list) => {
    const next = [...files, ...Array.from(list || [])].slice(0, MAX_ATTACHMENTS);
    setFiles(next);
    if (fileInput.current) fileInput.current.value = "";
  };

  const doCreate = async () => {
    setBusy(true);
    setError("");
    try {
      const attachments = [];
      for (const f of files) {
        attachments.push({ name: f.name, content_b64: await fileToB64(f) });
      }
      const result = await send("POST", "/missions", {
        instance,
        title: title.trim(),
        description,
        priority: supportsPriority ? priority : "medium",
        adopt: showAdopt ? adopt : true,
        attachments,
      });
      onCreated(result);
    } catch (e) {
      setError(String(e.message || e));
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  };

  const adoptionLine = !showAdopt
    ? "Every mission is adopted in this deployment — DevCake triages it on the next poll."
    : adopt
      ? "It carries the DEVCAKE label — DevCake starts ONBOARD on the next poll."
      : "It will NOT carry the DEVCAKE label — DevCake ignores it until you add the label in the PMO.";
  const confirmBody =
    `Creates the issue "${title.trim()}" on ${inst?.team_key || "?"} in ` +
    `${sysMeta?.display_name || inst?.system || "?"} (instance ${instance})` +
    `${files.length ? `, with ${files.length} attachment${files.length === 1 ? "" : "s"}` : ""}.` +
    `\n${adoptionLine}`;

  return (
    <Modal className="max-w-lg" onClose={busy ? undefined : onClose}>
      <h4 className="mb-1 text-base font-semibold tracking-tight">New mission</h4>
      <p className="mb-4 text-xs text-neutral-500 dark:text-neutral-400">
        Written straight to your PMO — DevCake keeps no copy. Title and
        description are redacted for secret shapes before they leave DevCake.
      </p>
      {pmos !== null && pmos.length === 0 && (
        <p className="mb-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/60 dark:text-amber-300">
          No configured PMO instance — connect one on the PMO page first.
        </p>
      )}
      <div className="space-y-3">
        {(pmos || []).length > 1 && (
          <Field label="PMO">
            <Select
              aria-label="PMO instance"
              value={instance}
              onChange={(e) => setInstance(e.target.value)}
            >
              {(pmos || []).map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name} — {p.team_key}
                </option>
              ))}
            </Select>
          </Field>
        )}
        <Field label="Title" hint="Short summary — the Dev sees this verbatim.">
          <Input
            autoFocus
            value={title}
            aria-label="Mission title"
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) =>
              e.key === "Enter" && !e.shiftKey && title.trim() && setConfirming(true)}
          />
        </Field>
        <Field label="Description" hint="Markdown. The full brief the Dev reads.">
          <Textarea
            rows={5}
            value={description}
            aria-label="Mission description"
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
        {supportsPriority && (
          <Field label="Priority">
            <Select
              aria-label="Mission priority"
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
            >
              {PRIORITIES.map((p) => (
                <option key={p.value} value={p.value}>{p.label}</option>
              ))}
            </Select>
          </Field>
        )}
        {showAdopt && (
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={adopt}
              onChange={(e) => setAdopt(e.target.checked)}
            />
            Start work on next poll
            <span className="text-xs text-neutral-500 dark:text-neutral-400">
              (adds the DEVCAKE opt-in label)
            </span>
          </label>
        )}
        <div>
          <input
            ref={fileInput}
            id="new-mission-files"
            type="file"
            multiple
            className="hidden"
            onChange={(e) => pickFiles(e.target.files)}
          />
          <label
            htmlFor="new-mission-files"
            className="cursor-pointer text-xs text-accent-700 underline underline-offset-2 dark:text-accent-300"
          >
            Attach files…
          </label>
          {files.length > 0 && (
            <ul className="mt-1.5 space-y-1">
              {files.map((f, i) => (
                <li key={`${f.name}-${i}`} className="flex items-center gap-2 text-xs text-neutral-600 dark:text-neutral-300">
                  <span className="truncate font-mono">{f.name}</span>
                  <span className="text-neutral-400 dark:text-neutral-500">
                    {(f.size / 1024).toFixed(0)} KB
                  </span>
                  <button
                    type="button"
                    aria-label={`Remove attachment ${f.name}`}
                    className="rounded text-neutral-400 hover:text-red-600 dark:hover:text-red-400"
                    onClick={() => setFiles(files.filter((_, j) => j !== i))}
                  >
                    ✕
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        {error && (
          <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <Button kind="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
          <Button
            disabled={busy || !title.trim() || !instance}
            onClick={() => setConfirming(true)}
          >
            Create mission…
          </Button>
        </div>
      </div>
      <ConfirmDialog
        open={confirming}
        title={`Create mission in "${instance}"?`}
        body={confirmBody}
        confirmLabel="Create mission"
        confirmKind="primary"
        busy={busy}
        onConfirm={doCreate}
        onCancel={() => !busy && setConfirming(false)}
      />
    </Modal>
  );
}
