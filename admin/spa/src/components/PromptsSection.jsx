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
import { MISSION_TYPES } from "../lib/missionStages.js";

// Per-Mission-Type prompt templates (v0.1.1). Template bodies create/edit/
// delete IMMEDIATELY (the dev-type precedent — the modal has its own explicit
// Save); only the ACTIVE selection rides the unified config draft
// (cfg.active_prompt_templates.<TYPE> → the page-level Save review).
// CAKE-150: per-PMO overrides ride cfg.pmos[i].active_prompt_templates
// (inherit/override idiom mirrors AssignmentsSection / ADR-0019).

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
  const pmos = cfg.pmos || [];
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

  // CAKE-150: presence = override; empty select value deletes the key to inherit
  const setPmoOverride = (i, p, mt, name) => {
    if (!name) {
      const { [mt]: _drop, ...rest } = p.active_prompt_templates || {};
      setField(`cfg.pmos.${i}.active_prompt_templates`, rest);
    } else {
      setField(`cfg.pmos.${i}.active_prompt_templates.${mt}`, name);
    }
  };

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
        <h4 className="border-b border-neutral-200 pb-1 text-sm font-semibold uppercase tracking-wide text-neutral-500 dark:border-neutral-800 dark:text-neutral-400">
          Mission Types
        </h4>
      )}
      {data && Object.keys(data.templates || {}).map((mt) => {
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
            <details className="group">
              <summary className="cursor-pointer list-none text-xs font-medium text-neutral-500 underline-offset-2 hover:underline dark:text-neutral-400">
                <span className="group-open:hidden">Manage templates ({entries.length})…</span>
                <span className="hidden group-open:inline">Hide templates</span>
              </summary>
              <ul className="mt-2 space-y-1">
                {entries.map((t) => (
                  <li key={t.name} className="flex flex-wrap items-center gap-2 text-sm">
                    <code className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs dark:bg-neutral-800">{t.name}</code>
                    {t.name === active && <span className="text-xs text-green-700 dark:text-green-400">active</span>}
                    <button type="button" className="text-xs text-accent-600 underline-offset-2 hover:underline"
                      onClick={() => { setViewMode("rendered"); setViewing({ mt, entry: t }); }}>View</button>
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
              {entries.some((t) => t.builtin) && (
                <p className="mt-1.5 text-xs text-neutral-500 dark:text-neutral-400">
                  Built-in templates are read-only and refreshed on upgrade.
                </p>
              )}
            </details>
          </div>
        );
      })}
      {data && pmos.length > 0 && (
        <>
          <h4 className="border-b border-neutral-200 pb-1 pt-2 text-sm font-semibold uppercase tracking-wide text-neutral-500 dark:border-neutral-800 dark:text-neutral-400">
            Per-PMO overrides
          </h4>
          <p className="text-sm text-neutral-500 dark:text-neutral-400">
            Each PMO can pick a different playbook for the same Mission Type.
            Inherit follows the global active above (live: a later global edit
            applies here too).
          </p>
          {pmos.map((p, i) => (
            <div key={p.name || i}
              className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
              <h4 className="text-sm font-semibold">
                Overrides — <span className="font-mono">{p.name || `PMO #${i + 1}`}</span>
                <Help text="A row set here replaces the global active template for this PMO instance only. Inherit rows follow the global Mission Types table above." />
              </h4>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[28rem] text-sm">
                  <tbody>
                    {MISSION_TYPES.map((mt) => {
                      const entries = data.templates?.[mt] || [];
                      const override = (p.active_prompt_templates || {})[mt] || "";
                      return (
                        <tr key={mt}
                          className="border-t border-neutral-100 dark:border-neutral-800">
                          <td className="w-32 py-2 font-mono text-xs font-semibold">{mt}</td>
                          <td className="py-2">
                            <Select value={override}
                              aria-label={`${p.name || `PMO #${i + 1}`} ${mt} template`}
                              onChange={(e) => setPmoOverride(i, p, mt, e.target.value)}>
                              <option value="">
                                Inherit (global: {activeOf(mt)})
                              </option>
                              {entries.map((t) => (
                                <option key={t.name} value={t.name}>
                                  {t.name}{t.builtin ? " (built-in)" : ""}
                                </option>
                              ))}
                            </Select>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </>
      )}
      {data && (
        <h4 className="border-b border-neutral-200 pb-1 pt-2 text-sm font-semibold uppercase tracking-wide text-neutral-500 dark:border-neutral-800 dark:text-neutral-400">
          Dev Types
        </h4>
      )}
      {data && Object.keys(data.dev_types || {}).map((n) => {
        const entries = data.dev_types?.[n] || [];
        const active = activeDevOf(n);
        return (
          <div key={`dev-${n}`} className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-mono text-sm font-semibold">{n} <span className="font-sans text-xs text-neutral-500 dark:text-neutral-400">(Dev Type identifying prompt)</span></span>
              <Button kind="ghost" onClick={() => setModal({ mt: n, kind: "dev" })}>
                + Create prompt template
              </Button>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label="Active template"
                help="The identifying prompt this Dev Type runs with, delivered before every playbook. Saved with the page-level Save.">
                <Select value={active}
                  onChange={(e) => setField(`cfg.active_devtype_prompts.${n}`, e.target.value)}>
                  {entries.map((t) => (
                    <option key={t.name} value={t.name}>{t.name}</option>
                  ))}
                </Select>
              </Field>
            </div>
            <details className="group">
              <summary className="cursor-pointer list-none text-xs font-medium text-neutral-500 underline-offset-2 hover:underline dark:text-neutral-400">
                <span className="group-open:hidden">Manage templates ({entries.length})…</span>
                <span className="hidden group-open:inline">Hide templates</span>
              </summary>
              <ul className="mt-2 space-y-1">
                {entries.map((t) => (
                  <li key={t.name} className="flex flex-wrap items-center gap-2 text-sm">
                    <code className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs dark:bg-neutral-800">{t.name}</code>
                    {t.name === active && <span className="text-xs text-green-700 dark:text-green-400">active</span>}
                    <button type="button" className="text-xs text-accent-600 underline-offset-2 hover:underline"
                      onClick={() => { setViewMode("rendered"); setViewing({ mt: n, entry: t }); }}>View</button>
                    <button type="button" className="text-xs text-accent-600 underline-offset-2 hover:underline"
                      onClick={() => setModal({ mt: n, kind: "dev", initial: t })}>Edit</button>
                    {t.name !== active && (
                      <button type="button" className="text-xs text-red-600 underline-offset-2 hover:underline dark:text-red-400"
                        onClick={() => remove(n, t.name, "dev")}>Delete</button>
                    )}
                  </li>
                ))}
              </ul>
            </details>
          </div>
        );
      })}
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
