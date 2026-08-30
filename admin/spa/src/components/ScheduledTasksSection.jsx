import React, { useState } from "react";
import { Play, Plus, Trash2 } from "lucide-react";
import { send } from "../api.js";
import { Section } from "./Card.jsx";
import { Field, Help, Input, Select, Textarea } from "./Field.jsx";
import SettingRow from "./SettingRow.jsx";
import Button from "./Button.jsx";
import Toggle from "./Toggle.jsx";
import ImmediateBadge from "./ImmediateBadge.jsx";
import { ConfirmDialog, Modal, PromptDialog } from "./Modal.jsx";
import { CRON_ID_RE, CRON_ID_RULE } from "../lib/draftErrors.js";
import { STAGES } from "../lib/missionStages.js";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";

// Scheduled Tasks (#/config/scheduled-tasks): the one home for everything
// that fires on a timer. Card 1 = DevCake tasks (built-in: Relations
// Steward + Memory Curator — permanent, never deletable). Card 2 = Custom
// tasks (operator cron rows). The card split IS the segregation.

const MEMORY_CURATOR = "memory-curator";

function DegradedPill() {
  return (
    <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-950 dark:text-amber-300">
      degraded
    </span>
  );
}

// Full-width editor for a task's instruction text — a multi-line prompt
// squeezed into a SettingRow's right column is unusable (founder,
// 2026-08-14); the big field gets the whole card width instead.
function PromptBlock({ label, desc, help, aria, value, onChange }) {
  return (
    <div className="pt-3">
      <div className="mb-2">
        <span className="text-sm font-medium">
          {label}
          {help && <Help text={help} />}
        </span>
        {desc && (
          <p className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">
            {desc}
          </p>
        )}
      </div>
      <Textarea className="min-h-40 w-full text-sm"
        aria-label={aria} value={value} onChange={onChange} />
    </div>
  );
}

function RunMsg({ msg }) {
  if (!msg) return null;
  return (
    <p className={`text-sm ${msg.startsWith("✗")
      ? "text-red-600 dark:text-red-400"
      : "text-green-700 dark:text-green-400"}`}>
      {msg}
    </p>
  );
}

// Shared card shell for the two built-in tasks: header row (name + Help +
// status line left; ImmediateBadge + Run now right), divide-y body,
// warnings/result lines below. Generalized from the Relations Steward
// panel that lived on Traffic control.
function BuiltinTaskPanel({ name, help, statusLine, degraded, runAria,
                            onRunNow, runDisabled, runTitle, runMsg,
                            children, footer }) {
  return (
    <div className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <span className="text-sm font-semibold">
            {name}
            <Help text={help} />
            {degraded && <span className="ml-2"><DegradedPill /></span>}
          </span>
          <p className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">
            {statusLine}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <ImmediateBadge text="runs with saved settings" />
          <Button kind="ghost" icon={Play} onClick={onRunNow}
            aria-label={runAria} disabled={runDisabled} title={runTitle}>
            Run now
          </Button>
        </div>
      </div>
      <div className="divide-y divide-neutral-100 dark:divide-neutral-800">
        {children}
      </div>
      {footer}
      <RunMsg msg={runMsg} />
    </div>
  );
}

function StewardPanel() {
  const { dr, healthInfo } = useSharedDraft();
  const [msg, setMsg] = useState("");
  const cfg = dr.draft.cfg;
  const setField = dr.setField;
  const rm = cfg.steward || { enabled: false, interval_minutes: 60, dev_type: null };
  const serverRm = dr.server.cfg.steward || {};
  const dirty = dr.diff.some((x) => x.path.startsWith("cfg.steward"));

  const run = async () => {
    setMsg("Starting…");
    try {
      const r = await send("POST", "/steward/run");
      setMsg(`✓ dispatched ${r.run_id} — watch it on the Runs page`);
    } catch (e) {
      setMsg(`✗ ${String(e.message || e)}`);
    }
  };

  return (
    <BuiltinTaskPanel
      name="Relations Steward"
      help="An AI worker that reads every open ticket's title and the start of its description, then proposes 'this one should wait for that one' orderings. It never edits code or tickets directly — DevCake validates each proposal and records it in your project tool, where deleting the relation undoes it."
      statusLine={rm.enabled
        ? `on · every ${rm.interval_minutes} min`
        : "off — manual only"}
      degraded={!!healthInfo?.steward_degraded}
      runAria="Run Relations Steward now"
      onRunNow={run}
      runDisabled={!serverRm.dev_type || dirty}
      runTitle={dirty
        ? "Save your steward changes first — Run now uses the saved settings"
        : !serverRm.dev_type ? "Pick and save a Dev Type first" : undefined}
      runMsg={msg}
      footer={healthInfo?.steward_degraded && (
        <p className="text-sm text-amber-600 dark:text-amber-400">
          Backing off — the last 3 steward runs failed
          ({healthInfo.steward_degraded}). Run now still works; a successful
          run resumes the schedule.
        </p>
      )}>
      <SettingRow label="Dev Type"
        desc="Which AI worker profile runs this task."
        help="Dev Types are the worker profiles you define under Configuration → Dev Types (which model, which coding harness). The built-in 'steward' profile is an EXECUTE-grade, judgment-heavy model (Opus-class) — point this task at a different Dev Type if you want a cheaper vehicle.">
        <Select className="w-44" value={rm.dev_type || ""}
          aria-label="Relations Steward Dev Type"
          onChange={(e) => {
            setField("cfg.steward.dev_type", e.target.value || null);
            if (!e.target.value) setField("cfg.steward.enabled", false);
          }}>
          <option value="">(none)</option>
          {dr.order.map((n) => <option key={n} value={n}>{n}</option>)}
        </Select>
      </SettingRow>
      <SettingRow label="Interval"
        desc="Minutes between automatic passes."
        help="How often the schedule fires when it is on. The first automatic pass happens one interval after DevCake starts; use Run now for an immediate one.">
        <Input type="number" className="w-24" min="1" value={rm.interval_minutes}
          aria-label="Relations Steward interval (minutes)"
          onChange={(e) => setField("cfg.steward.interval_minutes", Number(e.target.value))}
          onBlur={(e) => setField("cfg.steward.interval_minutes",
            Math.max(1, Number(e.target.value) || 60))} />
      </SettingRow>
      <SettingRow label="Periodic service"
        desc={rm.enabled ? "ON — runs on the interval." : "OFF — manual only (default)."}>
        <Toggle on={rm.enabled} label="Relations Steward periodic service"
          onClick={() => {
            if (!rm.enabled && !rm.dev_type) {
              setMsg("✗ pick a Dev Type first");
              return;
            }
            setField("cfg.steward.enabled", !rm.enabled);
          }} />
      </SettingRow>
      <PromptBlock label="Instructions"
        desc="What the steward is told to do. {mission_table} is replaced with your open tickets."
        help="This is the task's playbook, and you may rewrite it. Where you place {mission_table}, DevCake inserts the live list of open tickets (leave it out and the list is appended at the end). The technical output format the worker must produce is managed by DevCake and always appended after your text, so edits here cannot break the pipeline."
        aria="Relations Steward instructions"
        value={rm.playbook_template ?? ""}
        onChange={(e) => setField("cfg.steward.playbook_template",
          e.target.value)} />
    </BuiltinTaskPanel>
  );
}

function CuratorPanel() {
  const { dr, healthInfo } = useSharedDraft();
  const [msg, setMsg] = useState("");
  const cfg = dr.draft.cfg;
  const setField = dr.setField;
  const rows = cfg.crons || [];
  const idx = rows.findIndex((r) => r.id === MEMORY_CURATOR);
  if (idx < 0) return null;   // server always seeds it; belt only
  const row = rows[idx];
  const serverRow = (dr.server.cfg.crons || [])
    .find((r) => r.id === MEMORY_CURATOR);
  const dirty = JSON.stringify(row) !== JSON.stringify(serverRow);
  const noBoard = healthInfo?.memory_curator_no_board || [];
  const degraded = (healthInfo?.cron_degraded || []).includes(MEMORY_CURATOR);
  const depths = Object.entries(healthInfo?.claims_depth || {});

  const run = async () => {
    setMsg("Starting…");
    try {
      const r = await send("POST", `/cron/${MEMORY_CURATOR}/run`);
      const n = (r.created || []).length;
      setMsg(n
        ? `✓ ${r.created.map((c) => `${c.pmo}:${c.key}`).join(", ")}`
        : "✓ no ticket — boards paused or a curation ticket is still in flight");
    } catch (e) {
      setMsg(`✗ ${String(e.message || e)}`);
    }
  };

  return (
    <BuiltinTaskPanel
      name="Memory Curator"
      help="Team memory in DevCake is a notebook — a git repository of curated notes that workers consult while they work. As runs finish, DevCake copies their raw discoveries into the notebook's incoming queue. This task assigns a worker to review that queue: promote a lead into a proper note or discard it, always through a pull request you can inspect. It fires one ticket per Curator board (a board you set up whose only repository is a notebook). Automatic fires skip empty queues; Run now never skips — handy for a tidy-up pass. Context-sourcing strictness and memory auto-merge live under Configuration → Policies → Memory."
      statusLine={(row.enabled
        ? `on · every ${row.interval_minutes} min`
        : "off — manual only") + " · every Curator board"}
      degraded={degraded}
      runAria="Run Memory Curator now"
      onRunNow={run}
      runDisabled={dirty}
      runTitle={dirty
        ? "Save your changes first — Run now uses the saved settings"
        : undefined}
      runMsg={msg}
      footer={
        <>
          {noBoard.length > 0 && (
            <p className="text-sm text-amber-600 dark:text-amber-400">
              No Curator board for: {noBoard.join(", ")}. Create a PMO whose
              only work repository is that notebook.
            </p>
          )}
          {degraded && (
            <p className="text-sm text-amber-600 dark:text-amber-400">
              Automatic fires paused after 3 failed fires in a row. Run now
              still works; a successful fire resumes the schedule.
            </p>
          )}
          {depths.length > 0 && (
            <p className="text-xs text-neutral-500 dark:text-neutral-400">
              Queue depth:{" "}
              {depths.map(([card, n]) => `${card} ${n}`).join(" · ")}
            </p>
          )}
        </>
      }>
      <SettingRow label="Interval"
        desc="Minutes between automatic fires.">
        <Input type="number" className="w-24" min={1}
          value={row.interval_minutes ?? 60}
          aria-label="Memory Curator interval (minutes)"
          onChange={(e) => setField(`cfg.crons.${idx}.interval_minutes`,
            Number(e.target.value))}
          onBlur={(e) => setField(`cfg.crons.${idx}.interval_minutes`,
            Math.max(1, Number(e.target.value) || 60))} />
      </SettingRow>
      <SettingRow label="Periodic service"
        desc={row.enabled
          ? "ON — drains queues on the interval."
          : "OFF — manual only (default)."}>
        <Toggle on={!!row.enabled} label="Memory Curator periodic service"
          onClick={() => setField(`cfg.crons.${idx}.enabled`, !row.enabled)} />
      </SettingRow>
      <PromptBlock label="Instructions"
        desc="Becomes the description of each curation ticket. {timestamp} is replaced with the fire time."
        help="The worker on the Curator board receives this text as its assignment — edit it to change how incoming leads are reviewed and filed into notes. {timestamp} is the only substitution."
        aria="Memory Curator instructions"
        value={row.description_template || ""}
        onChange={(e) => setField(`cfg.crons.${idx}.description_template`,
          e.target.value)} />
    </BuiltinTaskPanel>
  );
}

// Modal editor for one custom task — edits the shared draft directly
// (DevTypeEditor doctrine: no per-modal save; the page-level Save applies).
function TaskEditor({ idx, onClose }) {
  const { dr } = useSharedDraft();
  const cfg = dr.draft.cfg;
  const row = (cfg.crons || [])[idx];
  if (!row) return null;
  const set = (k, v) => dr.setField(`cfg.crons.${idx}.${k}`, v);
  return (
    <Modal onClose={onClose} className="max-w-xl">
      <div className="mb-4 flex items-baseline justify-between gap-3">
        <h4 className="text-base font-semibold tracking-tight">
          {row.name || row.id}
        </h4>
        <span className="font-mono text-xs text-neutral-500 dark:text-neutral-400">
          {row.id}
        </span>
      </div>
      <div className="space-y-4">
        <Field label="Task name">
          <Input value={row.name || ""}
            aria-label={`Task ${row.id} name`}
            onChange={(e) => set("name", e.target.value)} />
        </Field>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <Field label="Target board"
            help="Which project board receives the ticket.">
            <Select value={row.pmo || ""}
              aria-label={`Task ${row.id} target board`}
              onChange={(e) => set("pmo", e.target.value)}>
              <option value="">—</option>
              {(cfg.pmos || []).map((p) => (
                <option key={p.name} value={p.name}>{p.name}</option>
              ))}
            </Select>
          </Field>
          <Field label="Entry stage"
            help="Where DevCake's pipeline starts for this ticket. EXECUTE is right for most chores — a worker just does the task. ONBOARD lets DevCake triage and plan it first; REVIEW only checks existing work.">
            <Select value={row.entry_stage || "EXECUTE"}
              aria-label={`Task ${row.id} entry stage`}
              onChange={(e) => set("entry_stage", e.target.value)}>
              {STAGES.map((s) => <option key={s} value={s}>{s}</option>)}
            </Select>
          </Field>
          <Field label="Interval (minutes)">
            <Input type="number" min={1} value={row.interval_minutes ?? 60}
              aria-label={`Task ${row.id} interval minutes`}
              onChange={(e) => set("interval_minutes", Number(e.target.value))}
              onBlur={(e) => set("interval_minutes",
                Math.max(1, Number(e.target.value) || 60))} />
          </Field>
        </div>
        <Field label="Ticket text"
          hint="Becomes the ticket description — write it like an assignment for a colleague. {timestamp} is replaced with the fire time.">
          <Textarea className="min-h-28 text-sm"
            aria-label={`Task ${row.id} ticket text`}
            value={row.description_template || ""}
            onChange={(e) => set("description_template", e.target.value)} />
        </Field>
      </div>
      <div className="mt-5 flex items-center justify-between gap-3">
        <p className="text-xs text-neutral-500 dark:text-neutral-400">
          Edits ride the draft — applied by the page-level Save.
        </p>
        <Button kind="ghost" onClick={onClose}>Close</Button>
      </div>
    </Modal>
  );
}

export default function ScheduledTasksSection() {
  const { dr, healthInfo } = useSharedDraft();
  const cfg = dr.draft.cfg;
  const setField = dr.setField;
  const rows = cfg.crons || [];
  const custom = rows
    .map((row, i) => ({ row, i }))
    .filter(({ row }) => row.id !== MEMORY_CURATOR && !row.reserved);
  const serverById = new Map(
    (dr.server.cfg.crons || []).map((r) => [r.id, r]));
  const degraded = new Set(healthInfo?.cron_degraded || []);

  const [runMsg, setRunMsg] = useState({});
  const [confirm, setConfirm] = useState(null);
  const [editIdx, setEditIdx] = useState(null);
  const [creating, setCreating] = useState(false);
  const [createErr, setCreateErr] = useState(null);

  const runNow = async (id) => {
    setRunMsg((m) => ({ ...m, [id]: "Starting…" }));
    try {
      const r = await send("POST", `/cron/${id}/run`);
      const n = (r.created || []).length;
      setRunMsg((m) => ({
        ...m,
        [id]: n
          ? `✓ ${r.created.map((c) => `${c.pmo}:${c.key}`).join(", ")}`
          : "✓ no ticket — board paused or the last ticket is still in flight",
      }));
    } catch (e) {
      setRunMsg((m) => ({ ...m, [id]: `✗ ${String(e.message || e)}` }));
    }
  };

  const addTask = (id) => {
    if (!CRON_ID_RE.test(id)) {
      setCreateErr(`"${id}" is invalid — ${CRON_ID_RULE}.`);
      return;
    }
    if (rows.some((r) => r.id === id)) {
      setCreateErr(`"${id}" is taken — task ids are unique.`);
      return;
    }
    setField("cfg.crons", [...rows, {
      id, name: id, enabled: false, interval_minutes: 60,
      pmo: (cfg.pmos || [])[0]?.name || "",
      entry_stage: "EXECUTE", description_template: "", reserved: false,
    }]);
    setCreating(false);
    setCreateErr(null);
    setEditIdx(rows.length);   // straight into the editor (create→edit flow)
  };

  const removeTask = (row, i) => {
    setConfirm({
      title: `Delete task "${row.name || row.id}"?`,
      body: "This task stops creating tickets after you Save. In-flight tickets are not canceled.",
      confirmLabel: "Remove from draft",
      action: () => {
        setField("cfg.crons", rows.filter((_, x) => x !== i));
        setConfirm(null);
      },
    });
  };

  return (
    <>
      <Section id="scheduled-tasks" title="DevCake tasks"
        description="Two built-in maintenance workers on a schedule. Settings wait for Save; Run now fires immediately."
        help="DevCake ships two housekeeping tasks. The Relations Steward reads your open tickets and proposes which should wait for which — the orderings land in your project tool, where you can delete any it got wrong. The Memory Curator tidies team memory: it turns raw leads that runs left behind into reviewed notes, one pull request at a time. Both are permanent and off by default. Run now uses the last SAVED settings, so it is disabled while a task has unsaved edits.">
        <StewardPanel />
        <CuratorPanel />
      </Section>

      <Section id="scheduled-tasks-custom" title="Custom tasks"
        description="Your own recurring chores — each fires one ticket onto a board, on a schedule or on demand."
        help="A scheduled task does exactly one thing: on its schedule (or when you press Run now) it creates a ticket on the board you chose, with your text as the description; DevCake's workers pick it up from the stage you chose. Use it for recurring chores — nightly triage, weekly dependency bumps, periodic reports. A fire is skipped while the previous ticket is still open or the board's intake is paused. Edits, new tasks and deletions wait for Save; Run now fires the saved row immediately."
        actions={<ImmediateBadge text="Run now is instant" />}>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[40rem] text-left text-sm">
            <thead className="text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
              <tr>
                <th className="py-1.5 pr-3">task</th>
                <th className="pr-3">on</th>
                <th className="pr-3">every</th>
                <th className="pr-3">target</th>
                <th className="pr-3">stage</th>
                <th className="pr-3 text-right">actions</th>
              </tr>
            </thead>
            <tbody>
              {custom.length === 0 && (
                <tr className="border-t border-neutral-100 dark:border-neutral-800">
                  <td colSpan={6}
                    className="py-6 text-center text-sm text-neutral-500 dark:text-neutral-400">
                    No custom tasks yet — recurring chores land here as one
                    labeled ticket per fire.
                  </td>
                </tr>
              )}
              {custom.map(({ row, i }) => {
                const serverRow = serverById.get(row.id);
                const dirty = !serverRow
                  || JSON.stringify(row) !== JSON.stringify(serverRow);
                return (
                  <tr key={row.id}
                    onClick={() => setEditIdx(i)}
                    title="Click to edit this task"
                    className="cursor-pointer border-t border-neutral-100 align-top hover:bg-stone-50 dark:border-neutral-800 dark:hover:bg-neutral-900">
                    <td className="py-2 pr-3">
                      <button type="button"
                        aria-label={`Edit task ${row.id}`}
                        className="block rounded text-left font-medium focus-visible:ring-2 focus-visible:ring-accent-500/60"
                        onClick={(e) => { e.stopPropagation(); setEditIdx(i); }}>
                        {row.name || row.id}
                      </button>
                      <div className="font-mono text-xs text-neutral-500 dark:text-neutral-400">
                        {row.id}
                      </div>
                      <div className="mt-0.5 flex flex-wrap gap-1">
                        {degraded.has(row.id) && <DegradedPill />}
                        {dirty && (
                          <span className="text-xs text-amber-700 dark:text-amber-300">
                            · unsaved changes
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="py-2 pr-3" onClick={(e) => e.stopPropagation()}>
                      <Toggle on={!!row.enabled} label={`Enable ${row.id}`}
                        onClick={() => setField(`cfg.crons.${i}.enabled`, !row.enabled)} />
                    </td>
                    <td className="py-2 pr-3 text-xs tabular-nums">
                      {row.interval_minutes ?? 60} min
                    </td>
                    <td className="py-2 pr-3">{row.pmo || "—"}</td>
                    <td className="py-2 pr-3 font-mono text-xs">
                      {row.entry_stage || "EXECUTE"}
                    </td>
                    <td className="py-2 pr-3" onClick={(e) => e.stopPropagation()}>
                      <div className="flex justify-end gap-1">
                        <Button kind="ghost" icon={Play} size="sm"
                          aria-label={`Run ${row.id} now`}
                          disabled={dirty}
                          title={dirty
                            ? (serverRow
                              ? "Save your changes first — Run now fires the saved row"
                              : "Save first — this task doesn't exist on the server yet")
                            : "Create a ticket now"}
                          onClick={() => runNow(row.id)}>
                          Run now
                        </Button>
                        <Button kind="danger-ghost" icon={Trash2} size="sm"
                          aria-label={`Delete ${row.id}`}
                          onClick={() => removeTask(row, i)}>
                          Delete
                        </Button>
                      </div>
                      {runMsg[row.id] && (
                        <div className="mt-1 text-right"><RunMsg msg={runMsg[row.id]} /></div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <button type="button"
          onClick={() => { setCreateErr(null); setCreating(true); }}
          className="flex w-full items-center justify-center gap-2 rounded-card border-2 border-dashed border-neutral-300 py-2.5 text-sm font-medium text-neutral-600 transition hover:border-accent-400 hover:bg-accent-50/40 hover:text-accent-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:border-neutral-700 dark:text-neutral-300 dark:hover:border-accent-700 dark:hover:bg-accent-950/30 dark:hover:text-accent-300">
          <Plus size={15} aria-hidden="true" />
          New scheduled task
        </button>
      </Section>

      <PromptDialog open={creating} title="New scheduled task"
        label="Task id"
        hint="Lowercase slug — letters, digits, - and _; up to 32 characters. Permanent; names the job in tickets and health."
        placeholder="nightly-triage"
        confirmLabel="Add to draft"
        error={createErr}
        onCancel={() => { setCreating(false); setCreateErr(null); }}
        onConfirm={addTask} />
      <ConfirmDialog open={!!confirm} title={confirm?.title}
        body={confirm?.body} confirmLabel={confirm?.confirmLabel}
        confirmKind="danger"
        onCancel={() => setConfirm(null)}
        onConfirm={confirm?.action} />
      {editIdx !== null && (
        <TaskEditor idx={editIdx} onClose={() => setEditIdx(null)} />
      )}
    </>
  );
}
