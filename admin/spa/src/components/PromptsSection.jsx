import React, { useEffect, useState } from "react";
import { get, send } from "../api.js";
import Button from "./Button.jsx";
import { Section } from "./Card.jsx";
import { ConfirmDialog, Modal } from "./Modal.jsx";
import { Field, Help, Input, Select, Textarea } from "./Field.jsx";
import ImmediateBadge from "./ImmediateBadge.jsx";
import SettingRow from "./SettingRow.jsx";
import MarkdownBody, {
  MarkdownModeToggle,
  MarkdownSourcePre,
  MarkdownViewShell,
} from "./MarkdownBody.jsx";
import { stripYamlFrontmatter } from "../lib/markdown.js";

// Per-Mission-Type prompt templates (v0.1.1). Template bodies create/edit/
// delete IMMEDIATELY (the dev-type precedent — the modal has its own explicit
// Save); only the ACTIVE selection rides the unified config draft
// (cfg.active_prompt_templates.<TYPE> → the page-level Save review).
const MISSION_TYPES = ["ONBOARD", "PLAN", "EXECUTE", "REVIEW"];

function TemplateModal({ mt, kind = "mission", variables, initial, onClose, onSaved }) {
  const editing = !!initial;
  const [name, setName] = useState(initial?.name || "");
  const [text, setText] = useState(initial?.template || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      const base = kind === "dev" ? "/devtype-prompts" : "/prompt-templates";
      await send("PUT", `${base}/${mt}/${encodeURIComponent(name)}`,
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
    <Modal className="max-w-3xl" onClose={busy ? undefined : onClose}>
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

// One compact row per group (Cursor-style settings list — the one-card-per-
// group layout repeated a big Create button and a half-width select six-plus
// times): identity left, active-template select right, template management
// and creation behind the row's disclosure. `canEdit`/`canDelete` carry the
// per-kind rules (mission: built-ins are read-only; dev: the active template
// cannot be deleted).
function PromptGroupRow({ name, tag, entries, active, help, builtinNote,
                          canEdit, canDelete, onActiveChange, onCreate,
                          onView, onEdit, onDelete }) {
  return (
    <div className="py-3 first:pt-1 last:pb-1">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
        <div className="min-w-0 sm:max-w-[34rem]">
          <span className="font-mono text-sm font-semibold">
            {name}
            {help && <Help text={help} />}
          </span>
          {tag && <p className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">{tag}</p>}
        </div>
        <Select className="w-full sm:w-56" aria-label={`Active template for ${name}`}
          value={active} onChange={onActiveChange}>
          {entries.map((t) => (
            <option key={t.name} value={t.name}>
              {t.name}{t.builtin ? " (built-in)" : ""}
            </option>
          ))}
        </Select>
      </div>
      <details className="group mt-1">
        <summary className="cursor-pointer list-none text-xs font-medium text-neutral-500 underline-offset-2 hover:underline dark:text-neutral-400">
          <span className="group-open:hidden">Manage templates ({entries.length})…</span>
          <span className="hidden group-open:inline">Hide templates</span>
        </summary>
        <div className="mt-2 space-y-2 rounded-md bg-stone-50 p-3 dark:bg-neutral-900">
          <ul className="space-y-1">
            {entries.map((t) => (
              <li key={t.name} className="flex flex-wrap items-center gap-2 text-sm">
                <code className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs dark:bg-neutral-800">{t.name}</code>
                {t.name === active && <span className="text-xs text-green-700 dark:text-green-400">active</span>}
                <button type="button" className="text-xs text-accent-600 underline-offset-2 hover:underline"
                  onClick={() => onView(t)}>View</button>
                {canEdit(t) && (
                  <button type="button" className="text-xs text-accent-600 underline-offset-2 hover:underline"
                    onClick={() => onEdit(t)}>Edit</button>
                )}
                {canDelete(t) && (
                  <button type="button" className="text-xs text-red-600 underline-offset-2 hover:underline dark:text-red-400"
                    onClick={() => onDelete(t)}>Delete</button>
                )}
              </li>
            ))}
          </ul>
          {builtinNote && entries.some((t) => t.builtin) && (
            <p className="text-xs text-neutral-500 dark:text-neutral-400">
              Built-in templates are read-only and refreshed on upgrade.
            </p>
          )}
          <Button kind="ghost" size="sm" onClick={onCreate}>+ New template…</Button>
        </div>
      </details>
    </div>
  );
}

export default function PromptsSection({ cfg, setField, devTypeNames = [] }) {
  const [data, setData] = useState(null);   // {variables, templates, active, dev_types, active_dev}
  const [modal, setModal] = useState(null); // {mt, kind, initial?}
  const [confirm, setConfirm] = useState(null);
  const [viewing, setViewing] = useState(null); // {mt, entry}
  const [viewMode, setViewMode] = useState("rendered"); // rendered | source
  const [workflow, setWorkflow] = useState("");
  const [switchNote, setSwitchNote] = useState("");
  const [err, setErr] = useState("");
  const [loadFailed, setLoadFailed] = useState(false);
  const refresh = () =>
    get("/prompt-templates")
      .then((d) => { setData(d); setLoadFailed(false); })
      .catch(() => { setData(null); setLoadFailed(true); });
  // re-fetch when the live Dev Type set changes — groups are API-driven,
  // never hardcoded, so a freshly created Dev appears immediately
  useEffect(() => { refresh(); }, [devTypeNames.sort().join(",")]);

  const activeOf = (mt) =>
    (cfg.active_prompt_templates || {})[mt] || (data?.active?.[mt]) || "Development";
  const activeDevOf = (n) =>
    (cfg.active_devtype_prompts || {})[n] || (data?.active_dev?.[n]) || "Development";

  // union of template names across every group — the workflow switcher's
  // options (item 7): applying sets each group's DRAFT where the name
  // exists and gracefully skips (and reports) the groups where it doesn't
  const allNames = data ? [...new Set([
    ...Object.values(data.templates || {}).flat().map((t) => t.name),
    ...Object.values(data.dev_types || {}).flat().map((t) => t.name),
  ])] : [];
  const applyWorkflow = () => {
    if (!workflow) return;
    const skipped = [];
    for (const mt of Object.keys(data.templates || {})) {
      if ((data.templates?.[mt] || []).some((t) => t.name === workflow))
        setField(`cfg.active_prompt_templates.${mt}`, workflow);
      else skipped.push(mt);
    }
    for (const n of Object.keys(data.dev_types || {})) {
      if ((data.dev_types[n] || []).some((t) => t.name === workflow))
        setField(`cfg.active_devtype_prompts.${n}`, workflow);
      else skipped.push(n);
    }
    setSwitchNote(skipped.length
      ? `Applied "${workflow}" where it exists — no template with that name for: ${skipped.join(", ")} (left unchanged). Save to persist.`
      : `Applied "${workflow}" to every group. Save to persist.`);
  };

  const remove = (mt, name, kind = "mission") =>
    setConfirm({
      title: `Delete template "${name}"?`,
      body: `The ${mt} template "${name}" is deleted immediately (anything using it falls back to Development).`,
      confirmLabel: "Delete",
      action: async () => {
        const base = kind === "dev" ? "/devtype-prompts" : "/prompt-templates";
        try {
          await send("DELETE", `${base}/${mt}/${encodeURIComponent(name)}`);
          setErr("");
        } catch (e) {
          // 409 when the template is still active somewhere
          setErr(`Could not delete "${name}": ${String(e.message || e).replace(/^\d+ /, "")}`);
        }
        setConfirm(null);
        refresh();
      },
    });

  return (
    <Section id="prompts" title="Prompts"
      description="The playbook DevCake sends a Dev for each Mission Type."
      help="The built-in default is read-only and refreshed on upgrade — create a template (copy the default) to customize, then select it as active."
      actions={<ImmediateBadge text="templates apply immediately; the active selection saves with the page" />}>
      {err && (
        <p className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300">
          ✗ {err}
        </p>
      )}
      {!data && (loadFailed ? (
        <p className="text-sm text-red-700 dark:text-red-300">
          Couldn&apos;t load the prompt templates.{" "}
          <button type="button" onClick={refresh}
            className="font-medium underline underline-offset-2">
            Retry
          </button>
        </p>
      ) : (
        <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading templates…</p>
      ))}
      {data && (
        <div className="rounded-card border border-accent-200 bg-accent-50/40 px-4 py-1 dark:border-accent-900 dark:bg-accent-950/20">
          <SettingRow label="Workflow switcher"
            desc="Set every group below to one stored template name in a single click."
            help="e.g. Development ↔ Customer Success. Groups without a template of that name are skipped. Drafted — nothing applies until Save.">
            <Select className="w-48" value={workflow}
              aria-label="Workflow template name"
              onChange={(e) => setWorkflow(e.target.value)}>
              <option value="">choose a workflow…</option>
              {allNames.map((n) => <option key={n} value={n}>{n}</option>)}
            </Select>
            <Button disabled={!workflow} onClick={applyWorkflow}>Apply to all</Button>
          </SettingRow>
          {switchNote && <p className="pb-2 text-xs text-amber-600 dark:text-amber-400">{switchNote}</p>}
        </div>
      )}
      {data && (
        <div>
          <h4 className="border-b border-neutral-200 pb-1 text-sm font-semibold uppercase tracking-wide text-neutral-500 dark:border-neutral-800 dark:text-neutral-400">
            Mission types
          </h4>
          <div className="divide-y divide-neutral-100 dark:divide-neutral-800">
            {Object.keys(data.templates || {}).map((mt) => (
              <PromptGroupRow key={mt} name={mt}
                entries={data.templates?.[mt] || []}
                active={activeOf(mt)}
                help="Which playbook this Mission Type dispatches with. Saved with the page-level Save; if the template file disappears, dispatch falls back to the built-in default and /health warns."
                builtinNote
                canEdit={(t) => !t.builtin}
                canDelete={(t) => !t.builtin}
                onActiveChange={(e) => setField(`cfg.active_prompt_templates.${mt}`, e.target.value)}
                onCreate={() => setModal({ mt })}
                onView={(t) => { setViewMode("rendered"); setViewing({ mt, entry: t }); }}
                onEdit={(t) => setModal({ mt, initial: t })}
                onDelete={(t) => remove(mt, t.name)} />
            ))}
          </div>
        </div>
      )}
      {data && (
        <div>
          <h4 className="border-b border-neutral-200 pb-1 text-sm font-semibold uppercase tracking-wide text-neutral-500 dark:border-neutral-800 dark:text-neutral-400">
            Dev types
          </h4>
          <div className="divide-y divide-neutral-100 dark:divide-neutral-800">
            {Object.keys(data.dev_types || {}).map((n) => (
              <PromptGroupRow key={`dev-${n}`} name={n}
                tag="Identifying prompt — delivered before every playbook."
                entries={data.dev_types?.[n] || []}
                active={activeDevOf(n)}
                help="The identifying prompt this Dev Type runs with, delivered before every playbook. Saved with the page-level Save."
                canEdit={() => true}
                canDelete={(t) => t.name !== activeDevOf(n)}
                onActiveChange={(e) => setField(`cfg.active_devtype_prompts.${n}`, e.target.value)}
                onCreate={() => setModal({ mt: n, kind: "dev" })}
                onView={(t) => { setViewMode("rendered"); setViewing({ mt: n, entry: t }); }}
                onEdit={(t) => setModal({ mt: n, kind: "dev", initial: t })}
                onDelete={(t) => remove(n, t.name, "dev")} />
            ))}
          </div>
        </div>
      )}
      {modal && (
        <TemplateModal mt={modal.mt} kind={modal.kind || "mission"} initial={modal.initial}
          variables={data?.variables?.[modal.mt] || []}
          onClose={() => setModal(null)}
          onSaved={() => { setModal(null); refresh(); }} />
      )}
      {viewing && (
        <Modal className="max-w-3xl" onClose={() => setViewing(null)}>
          <div className="mb-3 flex items-start justify-between gap-3">
            <h4 className="text-base font-semibold tracking-tight">
              {viewing.mt} · {viewing.entry.name}
            </h4>
            <MarkdownModeToggle mode={viewMode} onChange={setViewMode} />
          </div>
          <MarkdownViewShell>
            {viewMode === "rendered" ? (
              <MarkdownBody>
                {stripYamlFrontmatter(viewing.entry.template || "")}
              </MarkdownBody>
            ) : (
              <MarkdownSourcePre>{viewing.entry.template}</MarkdownSourcePre>
            )}
          </MarkdownViewShell>
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
