import React, { useEffect, useState } from "react";
import { get, send } from "../api.js";
import Button from "./Button.jsx";
import { Section } from "./Card.jsx";
import { ConfirmDialog, Modal } from "./Modal.jsx";
import { Field, Input, Select, Textarea } from "./Field.jsx";
import ImmediateBadge from "./ImmediateBadge.jsx";

// Per-Mission-Type prompt templates (v0.1.1). Template bodies create/edit/
// delete IMMEDIATELY (the dev-type precedent — the modal has its own explicit
// Save); only the ACTIVE selection rides the unified config draft
// (cfg.active_prompt_templates.<TYPE> → the page-level Save review).
const MISSION_TYPES = ["ONBOARD", "PLAN", "EXECUTE", "REVIEW"];

function TemplateModal({ mt, variables, initial, onClose, onSaved }) {
  const editing = !!initial;
  const [name, setName] = useState(initial?.name || "");
  const [text, setText] = useState(initial?.template || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      await send("PUT", `/prompt-templates/${mt}/${encodeURIComponent(name)}`,
                 { template: text });
      onSaved();
    } catch (e) {
      // the 422 body lists the valid variables — surface it verbatim
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal className="max-w-3xl">
      <h4 className="mb-1 text-base font-semibold tracking-tight">
        {editing ? `Edit template "${initial.name}"` : "Create prompt template"} · {mt}
      </h4>
      <p className="mb-3 text-sm text-neutral-500 dark:text-neutral-400">
        Available variables (all other braces are literal — paste JSON freely):
      </p>
      <div className="mb-3 flex flex-wrap gap-1.5">
        {variables.map((v) => (
          <code key={v} className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs dark:bg-neutral-800">
            {"{" + v + "}"}
          </code>
        ))}
      </div>
      <div className="space-y-3">
        {!editing && (
          <Field label="Template name"
            hint="Letters/digits/dashes/underscores, ≤64 chars. Locked after creation.">
            <Input value={name} onChange={(e) => setName(e.target.value)}
              placeholder="e.g. terse, strict-tdd" />
          </Field>
        )}
        <Field label="Template">
          <Textarea rows={18} className="font-mono text-xs" value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Write or paste the playbook for this Mission Type…" />
        </Field>
        {err && <p className="text-sm text-red-600 dark:text-red-400">⚠ {err}</p>}
        <div className="flex justify-end gap-2">
          <Button kind="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
          <Button disabled={busy || !name || !text.trim()} onClick={save}>
            {busy ? "Saving…" : editing ? "Save template" : "Create template"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

export default function PromptsSection({ cfg, setField }) {
  const [data, setData] = useState(null);   // {variables, templates, active}
  const [modal, setModal] = useState(null); // {mt, initial?}
  const [confirm, setConfirm] = useState(null);
  const [viewing, setViewing] = useState(null); // {mt, entry}
  const refresh = () => get("/prompt-templates").then(setData).catch(() => setData(null));
  useEffect(() => { refresh(); }, []);

  const activeOf = (mt) =>
    (cfg.active_prompt_templates || {})[mt] || (data?.active?.[mt]) || "default";

  const remove = (mt, name) =>
    setConfirm({
      title: `Delete template "${name}"?`,
      body: `The ${mt} template "${name}" is deleted immediately (missions currently using it fall back to the built-in default).`,
      confirmLabel: "Delete",
      action: async () => {
        try {
          await send("DELETE", `/prompt-templates/${mt}/${encodeURIComponent(name)}`);
        } catch (e) {
          window.alert(String(e.message || e));   // 409 when active
        }
        setConfirm(null);
        refresh();
      },
    });

  return (
    <Section id="prompts" title="Prompts"
      description="The playbook DevCake sends a Dev for each Mission Type. The built-in default is read-only and refreshed on upgrade — create a template (copy the default) to customize, then select it as active."
      actions={<ImmediateBadge text="templates apply immediately; the active selection saves with the page" />}>
      {!data && <p className="text-sm text-neutral-400">Loading templates…</p>}
      {data && MISSION_TYPES.map((mt) => {
        const entries = data.templates?.[mt] || [];
        const active = activeOf(mt);
        return (
          <div key={mt} className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-mono text-sm font-semibold">{mt}</span>
              <Button kind="ghost" onClick={() => setModal({ mt })}>
                + Create prompt template
              </Button>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label="Active template"
                help="Which playbook this Mission Type dispatches with. Saved with the page-level Save; if the template file disappears, dispatch falls back to the built-in default and /health warns.">
                <Select value={active}
                  onChange={(e) => setField(`cfg.active_prompt_templates.${mt}`, e.target.value)}>
                  {entries.map((t) => (
                    <option key={t.name} value={t.name}>
                      {t.name}{t.builtin ? " (built-in)" : ""}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <ul className="space-y-1">
              {entries.map((t) => (
                <li key={t.name} className="flex flex-wrap items-center gap-2 text-sm">
                  <code className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs dark:bg-neutral-800">{t.name}</code>
                  {t.builtin && <span className="text-xs text-neutral-400">read-only, refreshed on upgrade</span>}
                  {t.name === active && <span className="text-xs text-green-700 dark:text-green-400">active</span>}
                  <button type="button" className="text-xs text-accent-600 underline-offset-2 hover:underline"
                    onClick={() => setViewing({ mt, entry: t })}>View</button>
                  {!t.builtin && (
                    <>
                      <button type="button" className="text-xs text-accent-600 underline-offset-2 hover:underline"
                        onClick={() => setModal({ mt, initial: t })}>Edit</button>
                      <button type="button" className="text-xs text-red-600 underline-offset-2 hover:underline dark:text-red-400"
                        onClick={() => remove(mt, t.name)}>Delete</button>
                    </>
                  )}
                </li>
              ))}
            </ul>
          </div>
        );
      })}
      {modal && (
        <TemplateModal mt={modal.mt} initial={modal.initial}
          variables={data?.variables?.[modal.mt] || []}
          onClose={() => setModal(null)}
          onSaved={() => { setModal(null); refresh(); }} />
      )}
      {viewing && (
        <Modal className="max-w-3xl">
          <h4 className="mb-2 text-base font-semibold tracking-tight">
            {viewing.mt} · {viewing.entry.name}
          </h4>
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded bg-neutral-50 p-3 font-mono text-xs dark:bg-neutral-950">
            {viewing.entry.template}
          </pre>
          <div className="mt-3 flex justify-end">
            <Button kind="ghost" onClick={() => setViewing(null)}>Close</Button>
          </div>
        </Modal>
      )}
      <ConfirmDialog open={!!confirm} {...(confirm || {})}
        onConfirm={() => confirm.action()} onCancel={() => setConfirm(null)} />
    </Section>
  );
}
