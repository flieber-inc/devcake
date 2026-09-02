import React, { useEffect, useMemo, useState } from "react";
import { get, send } from "../api.js";
import Button from "./Button.jsx";
import { Section } from "./Card.jsx";
import { ConfirmDialog, Modal, PromptDialog } from "./Modal.jsx";
import { Field, Help, Input, Select, Textarea } from "./Field.jsx";
import ImmediateBadge from "./ImmediateBadge.jsx";
import MarkdownBody, {
  MarkdownModeToggle,
  MarkdownSourcePre,
  MarkdownViewShell,
} from "./MarkdownBody.jsx";
import { stripYamlFrontmatter } from "../lib/markdown.js";
import { MISSION_TYPES } from "../lib/missionStages.js";
import {
  pmoHasPromptOverride,
  pmoOverrideExpandIndexes,
  pmoOverrideSummaryText,
} from "../lib/pmoPromptOverrides.js";
import { templateSoftWarnings } from "../lib/templateSoftWarnings.js";

// Per-Mission-Type prompt templates. Template bodies create/edit/delete
// IMMEDIATELY (modal Save); only the ACTIVE selection rides the unified
// config draft. CAKE-150: per-PMO overrides on cfg.pmos[i].active_prompt_templates.
// CAKE-166: slim active rows + one Manage-templates modal (no Workflow switcher).
// CAKE-173: three Policies-style domain cards (Templates / Mission Types / Dev Types).

function TemplateManagerModal({
  data,
  cfg,
  initialKind = "mission",
  initialType = "",
  onClose,
  onChanged,
}) {
  const missionTypes = Object.keys(data.templates || {});
  const devTypes = Object.keys(data.dev_types || {});
  const [kind, setKind] = useState(
    initialKind === "dev" && devTypes.length ? "dev" : "mission",
  );
  const typeOptions = kind === "dev" ? devTypes : missionTypes;
  const [typeKey, setTypeKey] = useState(() => {
    if (initialType && typeOptions.includes(initialType)) return initialType;
    return typeOptions[0] || "";
  });
  const entries = useMemo(() => {
    if (!typeKey) return [];
    return kind === "dev"
      ? (data.dev_types?.[typeKey] || [])
      : (data.templates?.[typeKey] || []);
  }, [data, kind, typeKey]);

  const [selected, setSelected] = useState(() => entries[0]?.name || "");
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [text, setText] = useState("");
  const [viewMode, setViewMode] = useState("rendered");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [softWarns, setSoftWarns] = useState([]);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [duplicateOpen, setDuplicateOpen] = useState(false);
  const [duplicateErr, setDuplicateErr] = useState(null);
  const [duplicateHint, setDuplicateHint] = useState(null);

  // When kind changes, reset type to first available
  useEffect(() => {
    const opts = kind === "dev" ? Object.keys(data.dev_types || {})
      : Object.keys(data.templates || {});
    setTypeKey((prev) => (opts.includes(prev) ? prev : (opts[0] || "")));
  }, [kind, data]);

  // When type/entries change, keep selection if still present. Do not clear
  // softWarns from an effect keyed on `creating` / `entry?.name` — that wiped
  // Create-save amber warnings (CAKE-166 REVIEW). Clear only when the current
  // selection actually left the list (delete/refresh), via handlers otherwise.
  useEffect(() => {
    if (creating) return;
    if (entries.some((e) => e.name === selected)) return;
    setSelected(entries[0]?.name || "");
    setSoftWarns([]);
    setErr(null);
    // `creating` is read as a guard only; listing it as a dep re-ran this on
    // Create→Save and is unnecessary while startCreate/cancelCreate/save own
    // that transition.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entries, selected]);

  const entry = creating ? null : entries.find((e) => e.name === selected) || null;
  const editable = creating || (entry && !entry.builtin);
  const activeName = kind === "dev"
    ? ((cfg.active_devtype_prompts || {})[typeKey]
      || (data.active_dev || {})[typeKey] || "Development")
    : ((cfg.active_prompt_templates || {})[typeKey]
      || (data.active || {})[typeKey] || "Development");
  const canDelete = !!entry && !entry.builtin && entry.name !== activeName;
  const variables = (data.variables?.[typeKey] || []);

  // Sync editor from the selected entry. Soft warnings are owned by Save —
  // do not clear them here or a post-save refresh would hide them.
  useEffect(() => {
    if (creating) return;
    setText(entry?.template || "");
    setErr(null);
  }, [entry?.name, entry?.template, creating]); // eslint-disable-line react-hooks/exhaustive-deps

  const startCreate = () => {
    setCreating(true);
    setNewName("");
    setText(entry?.template || "");
    setViewMode("source");
    setSoftWarns([]);
    setErr(null);
    setDuplicateHint(null);
  };

  const cancelCreate = () => {
    setCreating(false);
    setNewName("");
    setSoftWarns([]);
    setErr(null);
  };

  const basePath = kind === "dev" ? "/devtype-prompts" : "/prompt-templates";
  const saveName = creating ? newName.trim() : (entry?.name || "");

  const openDuplicate = () => {
    if (!entry?.builtin) return;
    setDuplicateErr(null);
    setDuplicateOpen(true);
  };

  const confirmDuplicate = async (name) => {
    if (!entry?.builtin || !typeKey) return;
    const body = entry.template || text || "";
    if (!body.trim()) {
      setDuplicateErr("Built-in template body is empty — nothing to copy.");
      return;
    }
    setBusy(true);
    setDuplicateErr(null);
    setErr(null);
    try {
      await send("PUT",
        `${basePath}/${typeKey}/${encodeURIComponent(name)}`,
        { template: body });
      const soft = kind === "mission"
        ? templateSoftWarnings({
          missionType: typeKey,
          templateName: name,
          text: body,
          maxDecompositionDepth: cfg?.max_decomposition_depth,
          planApproval: (cfg?.pmos || []).some((p) => !!p.plan_approval),
        })
        : [];
      await onChanged();
      setDuplicateOpen(false);
      setCreating(false);
      setNewName("");
      setSelected(name);
      setViewMode("source");
      setSoftWarns(soft);
      setDuplicateHint(
        `"${name}" is saved and editable here, but it is not active until you `
        + "select it in the slim active row and click page Save.",
      );
    } catch (e) {
      setDuplicateErr(String(e.message || e).replace(/^\d+ /, ""));
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!typeKey || !saveName || !text.trim()) return;
    setBusy(true);
    setErr(null);
    const soft = kind === "mission"
      ? templateSoftWarnings({
        missionType: typeKey,
        templateName: saveName,
        text,
        maxDecompositionDepth: cfg?.max_decomposition_depth,
        planApproval: (cfg?.pmos || []).some((p) => !!p.plan_approval),
      })
      : [];
    setSoftWarns(soft);
    try {
      await send("PUT", `${basePath}/${typeKey}/${encodeURIComponent(saveName)}`,
        { template: text });
      const createdName = saveName;
      // Refresh first, select the saved name, then exit create mode — so the
      // editor sync effect lands on the new entry in one step and soft warns
      // set above are not cleared by a creating/entry?.name effect.
      await onChanged();
      setSelected(createdName);
      setCreating(false);
      setNewName("");
      setSoftWarns(soft);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const remove = () => {
    if (!entry || entry.builtin) return;
    setConfirmDelete({
      title: `Delete template "${entry.name}"?`,
      body: `The ${typeKey} template "${entry.name}" is deleted immediately `
        + "(anything using it falls back to Development).",
      confirmLabel: "Delete",
      action: async () => {
        setBusy(true);
        setErr(null);
        try {
          await send("DELETE",
            `${basePath}/${typeKey}/${encodeURIComponent(entry.name)}`);
          setConfirmDelete(null);
          await onChanged();
        } catch (e) {
          setErr(`Could not delete "${entry.name}": `
            + String(e.message || e).replace(/^\d+ /, ""));
          setConfirmDelete(null);
        } finally {
          setBusy(false);
        }
      },
    });
  };

  return (
    <>
      <Modal className="max-w-3xl" onClose={busy ? undefined : onClose}>
        <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h4 className="text-base font-semibold tracking-tight">
              Manage templates
            </h4>
            <p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">
              Body create/edit/delete apply immediately. Changing which template
              is <em>active</em> still requires page Save. The editable body is
              the operator playbook half — code-owned contract epilogues are
              assembled at dispatch and are not part of this editor.
            </p>
          </div>
          <MarkdownModeToggle mode={viewMode} onChange={setViewMode} />
        </div>

        <div className="mb-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label="Kind">
            <Select aria-label="Template kind" value={kind}
              onChange={(e) => {
                setKind(e.target.value);
                setCreating(false);
                setSoftWarns([]);
                setErr(null);
                setDuplicateHint(null);
              }}>
              <option value="mission">Mission Type</option>
              <option value="dev" disabled={!devTypes.length}>Dev Type</option>
            </Select>
          </Field>
          <Field label={kind === "dev" ? "Dev Type" : "Mission Type"}>
            <Select aria-label="Template type" value={typeKey}
              onChange={(e) => {
                setTypeKey(e.target.value);
                setCreating(false);
                setSoftWarns([]);
                setErr(null);
                setDuplicateHint(null);
              }}>
              {typeOptions.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </Select>
          </Field>
          <Field label="Template">
            {creating ? (
              <Input value={newName} onChange={(e) => setNewName(e.target.value)}
                placeholder="e.g. terse, strict-tdd"
                aria-label="New template name" />
            ) : (
              <Select aria-label="Template name" value={selected}
                onChange={(e) => {
                  setSelected(e.target.value);
                  setSoftWarns([]);
                  setErr(null);
                  setDuplicateHint(null);
                }}>
                {entries.map((t) => (
                  <option key={t.name} value={t.name}>
                    {t.name}{t.builtin ? " (built-in)" : ""}
                  </option>
                ))}
              </Select>
            )}
          </Field>
        </div>

        {kind === "mission" && variables.length > 0 && (
          <div className="mb-3">
            <p className="mb-1.5 text-xs text-neutral-500 dark:text-neutral-400">
              Available variables (all other braces are literal):
            </p>
            <div className="flex flex-wrap gap-1.5">
              {variables.map((v) => (
                <code key={v}
                  className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs dark:bg-neutral-800">
                  {"{" + v + "}"}
                </code>
              ))}
            </div>
          </div>
        )}

        {entry?.builtin && !creating && (
          <p className="mb-3 text-xs text-neutral-500 dark:text-neutral-400">
            Built-in templates are read-only and refreshed on upgrade — use
            {" "}<strong>Duplicate to edit</strong> to create an editable operator
            copy.
          </p>
        )}
        {duplicateHint && (
          <p className="mb-3 rounded-md border border-neutral-200 bg-stone-50 px-3 py-2 text-sm text-neutral-700 dark:border-neutral-700 dark:bg-neutral-800/60 dark:text-neutral-200"
            data-testid="duplicate-active-hint">
            {duplicateHint}
          </p>
        )}

        <MarkdownViewShell>
          {viewMode === "rendered" ? (
            <MarkdownBody>
              {stripYamlFrontmatter(text || "")}
            </MarkdownBody>
          ) : editable ? (
            <Textarea rows={18}
              className="min-h-[40vh] border-0 bg-transparent font-mono text-xs shadow-none focus:ring-0"
              value={text}
              onChange={(e) => { setText(e.target.value); setSoftWarns([]); }}
              aria-label="Template source"
              placeholder="Write or paste the playbook…" />
          ) : (
            <MarkdownSourcePre>{text}</MarkdownSourcePre>
          )}
        </MarkdownViewShell>

        {err && (
          <p className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300">
            ⚠ {err}
          </p>
        )}
        {softWarns.length > 0 && (
          <div className="mt-3 space-y-1.5 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-200"
            data-testid="template-soft-warnings">
            {softWarns.map((w) => (
              <p key={w}>⚠ {w}</p>
            ))}
            <p className="text-xs opacity-90">
              Saved anyway — these are the same soft warnings Overview/health
              surfaces; they do not block the write.
            </p>
          </div>
        )}

        <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap gap-2">
            {!creating && (
              <Button kind="ghost" disabled={busy || !typeKey} onClick={startCreate}>
                + Create
              </Button>
            )}
            {creating && (
              <Button kind="ghost" disabled={busy} onClick={cancelCreate}>
                Cancel create
              </Button>
            )}
            {!creating && canDelete && (
              <Button kind="danger-ghost" disabled={busy} onClick={remove}>
                Delete
              </Button>
            )}
          </div>
          <div className="flex gap-2">
            <Button kind="ghost" disabled={busy} onClick={onClose}>Close</Button>
            {entry?.builtin && !creating && (
              <Button
                disabled={busy || !typeKey}
                onClick={openDuplicate}
                data-testid="duplicate-to-edit"
              >
                Duplicate to edit
              </Button>
            )}
            {editable && (
              <Button disabled={busy || !saveName || !text.trim()} onClick={save}>
                {busy ? "Saving…" : creating ? "Create template" : "Save template"}
              </Button>
            )}
          </div>
        </div>
      </Modal>
      <PromptDialog
        open={duplicateOpen}
        title={`Duplicate "${entry?.name || "template"}"`}
        label="New template name"
        initial={entry?.name ? `${entry.name}-custom` : ""}
        placeholder="e.g. Development-custom"
        hint="Saves immediately as an operator copy. Select it as active on the Prompts page and click page Save for it to take effect."
        confirmLabel="Create copy"
        busy={busy}
        error={duplicateErr}
        onConfirm={confirmDuplicate}
        onCancel={() => { if (!busy) { setDuplicateOpen(false); setDuplicateErr(null); } }}
      />
      <ConfirmDialog open={!!confirmDelete} {...(confirmDelete || {})}
        onConfirm={() => confirmDelete?.action()}
        onCancel={() => setConfirmDelete(null)} />
    </>
  );
}

function SlimActiveRow({ name, subtitle, active, entries, onChange, ariaLabel }) {
  return (
    <div data-testid="prompt-active-row"
      className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-card border border-neutral-200 px-4 py-2.5 dark:border-neutral-800">
      <span className="min-w-0 font-mono text-sm font-semibold">
        {name}
        {subtitle && (
          <span className="ml-2 font-sans text-xs font-normal text-neutral-500 dark:text-neutral-400">
            {subtitle}
          </span>
        )}
      </span>
      <Select className="ml-auto w-full max-w-xs sm:w-56" value={active}
        aria-label={ariaLabel || `${name} active template`}
        onChange={(e) => onChange(e.target.value)}>
        {entries.map((t) => (
          <option key={t.name} value={t.name}>
            {t.name}{t.builtin ? " (built-in)" : ""}
          </option>
        ))}
      </Select>
    </div>
  );
}

export default function PromptsSection({ cfg, setField, devTypeNames = [] }) {
  const [data, setData] = useState(null);
  const [managerOpen, setManagerOpen] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [expandedOverrides, setExpandedOverrides] = useState(() => new Set());
  const pmos = cfg.pmos || [];
  const overrideSeedKey = pmos
    .map((p, i) => `${i}:${Object.entries(p.active_prompt_templates || {})
      .filter(([, v]) => !!v).map(([k]) => k).sort().join(",")}`)
    .join("|");
  useEffect(() => {
    const mustOpen = pmoOverrideExpandIndexes(pmos);
    if (mustOpen.length === 0) return;
    setExpandedOverrides((prev) => {
      let changed = false;
      const next = new Set(prev);
      for (const i of mustOpen) {
        if (!next.has(i)) { next.add(i); changed = true; }
      }
      return changed ? next : prev;
    });
  }, [overrideSeedKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const toggleOverrideCard = (i) => setExpandedOverrides((prev) => {
    const next = new Set(prev);
    if (next.has(i)) next.delete(i); else next.add(i);
    return next;
  });
  const refresh = () =>
    get("/prompt-templates")
      .then((d) => { setData(d); setLoadFailed(false); return d; })
      .catch(() => { setData(null); setLoadFailed(true); return null; });
  useEffect(() => { refresh(); }, [devTypeNames.sort().join(",")]);

  const activeOf = (mt) =>
    (cfg.active_prompt_templates || {})[mt] || (data?.active?.[mt]) || "Development";
  const activeDevOf = (n) =>
    (cfg.active_devtype_prompts || {})[n] || (data?.active_dev?.[n]) || "Development";

  const setPmoOverride = (i, p, mt, name) => {
    if (!name) {
      const { [mt]: _drop, ...rest } = p.active_prompt_templates || {};
      setField(`cfg.pmos.${i}.active_prompt_templates`, rest);
    } else {
      setField(`cfg.pmos.${i}.active_prompt_templates.${mt}`, name);
    }
  };

  // CAKE-173: three Policies-style domain cards (Templates / Mission Types /
  // Dev Types). Hooks, actives, overrides, and the modal stay at this root —
  // only the JSX shell splits. First card id matches the Fleet route key.
  return (
    <>
      <Section id="prompts" title="Templates"
        description="Create, edit, or delete playbook copies. The built-in default stays read-only."
        help="The built-in default is read-only and refreshed on upgrade — open Manage templates to create a copy, then select it as active on the cards below."
        actions={
          <>
            <ImmediateBadge text="template edits apply immediately" />
            <Button data-testid="manage-templates"
              disabled={!data}
              onClick={() => setManagerOpen(true)}>
              Manage templates
            </Button>
          </>
        }>
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
      </Section>

      <Section id="prompts-mission-types" title="Mission Types"
        description="Global active template per Mission Type, plus optional per-PMO exceptions. Active selection saves with the page.">
        {data && Object.keys(data.templates || {}).map((mt) => (
          <SlimActiveRow key={mt} name={mt}
            active={activeOf(mt)}
            entries={data.templates?.[mt] || []}
            onChange={(v) => setField(`cfg.active_prompt_templates.${mt}`, v)} />
        ))}
        {data && pmos.length > 0 && (
          <>
            <h4 className="border-b border-neutral-200 pb-1 pt-2 text-sm font-semibold uppercase tracking-wide text-neutral-500 dark:border-neutral-800 dark:text-neutral-400">
              Per-PMO overrides
            </h4>
            <p className="text-sm text-neutral-500 dark:text-neutral-400">
              Exceptions only — boards that inherit all four globals stay
              collapsed. Inherit follows the global active above (live: a later
              global edit applies here too).
            </p>
            {pmos.map((p, i) => {
              const label = p.name || `PMO #${i + 1}`;
              const overrides = p.active_prompt_templates || {};
              const hasOverride = pmoHasPromptOverride(p);
              const open = expandedOverrides.has(i);
              if (!open) {
                return (
                  <div key={p.name || i}
                    className="flex items-stretch rounded-card border border-neutral-200 dark:border-neutral-800">
                    <button type="button"
                      data-testid="pmo-prompt-override-summary"
                      aria-label={`Expand prompt overrides for ${label}`}
                      onClick={() => toggleOverrideCard(i)}
                      className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-left transition hover:bg-stone-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:hover:bg-neutral-900">
                      <span className="font-mono text-sm font-semibold">{label}</span>
                      <span className={`text-xs ${hasOverride
                        ? "text-neutral-600 dark:text-neutral-300"
                        : "text-neutral-400 dark:text-neutral-500"}`}>
                        {pmoOverrideSummaryText(overrides)}
                      </span>
                      <span aria-hidden className="ml-auto shrink-0 text-xs text-neutral-400">▸</span>
                    </button>
                  </div>
                );
              }
              return (
                <div key={p.name || i}
                  className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="flex items-center gap-2">
                      <button type="button"
                        aria-label={`Collapse prompt overrides for ${label}`}
                        title="Collapse to a summary row"
                        onClick={() => toggleOverrideCard(i)}
                        className="rounded text-xs text-neutral-400 hover:text-neutral-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:text-neutral-500 dark:hover:text-neutral-300">
                        ▾
                      </button>
                      <h4 className="text-sm font-semibold">
                        Overrides — <span className="font-mono">{label}</span>
                        <Help text="A row set here replaces the global active template for this PMO instance only. Inherit rows follow the global Mission Types actives above." />
                      </h4>
                    </span>
                  </div>
                  <div className="w-full min-w-0 max-w-full overflow-x-auto">
                    <table className="w-full min-w-[28rem] text-sm">
                      <tbody>
                        {MISSION_TYPES.map((mt) => {
                          const entries = data.templates?.[mt] || [];
                          const override = overrides[mt] || "";
                          return (
                            <tr key={mt}
                              className="border-t border-neutral-100 dark:border-neutral-800">
                              <td className="w-32 py-2 font-mono text-xs font-semibold">{mt}</td>
                              <td className="py-2">
                                <Select value={override}
                                  aria-label={`${label} ${mt} template`}
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
              );
            })}
          </>
        )}
        {!data && (
          <p className="text-sm text-neutral-500 dark:text-neutral-400">
            Waiting for templates to load…
          </p>
        )}
      </Section>

      <Section id="prompts-dev-types" title="Dev Types"
        description="Identifying-prompt active per Dev Type. Active selection saves with the page.">
        {data && Object.keys(data.dev_types || {}).map((n) => (
          <SlimActiveRow key={`dev-${n}`} name={n}
            subtitle="(Dev Type identifying prompt)"
            active={activeDevOf(n)}
            entries={data.dev_types?.[n] || []}
            onChange={(v) => setField(`cfg.active_devtype_prompts.${n}`, v)}
            ariaLabel={`${n} active Dev Type prompt`} />
        ))}
        {!data && (
          <p className="text-sm text-neutral-500 dark:text-neutral-400">
            Waiting for templates to load…
          </p>
        )}
      </Section>

      {managerOpen && data && (
        <TemplateManagerModal
          data={data}
          cfg={cfg}
          onClose={() => setManagerOpen(false)}
          onChanged={refresh} />
      )}
    </>
  );
}
