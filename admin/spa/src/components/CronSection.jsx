import React, { useState } from "react";
import { Play, Plus, Trash2 } from "lucide-react";
import { send } from "../api.js";
import { Section } from "./Card.jsx";
import { Input, Select, Textarea } from "./Field.jsx";
import SettingRow from "./SettingRow.jsx";
import Button from "./Button.jsx";
import Toggle from "./Toggle.jsx";
import ImmediateBadge from "./ImmediateBadge.jsx";
import { ConfirmDialog } from "./Modal.jsx";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";

const STAGES = ["ONBOARD", "PLAN", "EXECUTE", "REVIEW"];

export default function CronSection() {
  const { dr, healthInfo } = useSharedDraft();
  const cfg = dr.draft.cfg;
  const setField = dr.setField;
  const rows = cfg.crons || [];
  const [runMsg, setRunMsg] = useState({});
  const [confirm, setConfirm] = useState(null);

  const setRow = (i, patch) => {
    const next = rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r));
    setField("cfg.crons", next);
  };

  const runNow = async (id) => {
    setRunMsg((m) => ({ ...m, [id]: "Starting…" }));
    try {
      const r = await send("POST", `/cron/${id}/run`);
      const n = (r.created || []).length;
      const detail = n
        ? (r.created.map((c) => `${c.pmo}:${c.key}`).join(", "))
        : "no ticket (paused or already in flight)";
      setRunMsg((m) => ({ ...m, [id]: `✓ ${detail}` }));
    } catch (e) {
      setRunMsg((m) => ({ ...m, [id]: `✗ ${String(e.message || e)}` }));
    }
  };

  const addRow = () => {
    setField("cfg.crons", [...rows, {
      id: "job",
      name: "New cron",
      enabled: false,
      interval_minutes: 60,
      pmo: (cfg.pmos || [])[0]?.name || "",
      entry_stage: "EXECUTE",
      description_template: "",
      reserved: false,
    }]);
  };

  const removeRow = (i) => {
    const row = rows[i];
    if (row.reserved) return;
    setConfirm({
      title: `Delete cron "${row.name || row.id}"?`,
      body: "This job stops creating tickets after you Save. In-flight tickets are not canceled.",
      confirmLabel: "Remove from draft",
      danger: true,
      action: () => {
        setField("cfg.crons", rows.filter((_, idx) => idx !== i));
        setConfirm(null);
      },
    });
  };

  const degraded = new Set(healthInfo.cron_degraded || []);
  const noBoard = healthInfo.memory_curator_no_board || [];

  return (
    <>
      <Section id="cron" title="Cron"
        description="One verb: create a labeled ticket. List edits ride the draft until Save. Run now fires immediately against the saved row. Memory Curator fans out to every Curator board (one ticket per notebook); those pipelines share the global concurrency cap."
        actions={
          <Button kind="ghost" icon={Plus} onClick={addRow}>Add cron</Button>
        }>
        {noBoard.length > 0 && (
          <p className="mb-3 text-sm text-amber-700 dark:text-amber-300">
            Memory Curator has no board for: {noBoard.join(", ")}. Create a
            PMO whose only work repo is that notebook.
          </p>
        )}
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-neutral-500">
              <tr>
                <th className="py-1 pr-3">Job</th>
                <th className="pr-3">On</th>
                <th className="pr-3">Every</th>
                <th className="pr-3">Target</th>
                <th className="pr-3">Stage</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={`${row.id}-${i}`}
                  className="border-t border-neutral-100 dark:border-neutral-800">
                  <td className="py-2 pr-3">
                    <div className="font-medium">{row.name || row.id}</div>
                    <div className="font-mono text-xs text-neutral-500">{row.id}</div>
                    {row.reserved && (
                      <div className="text-xs text-neutral-500">reserved</div>
                    )}
                    {degraded.has(row.id) && (
                      <div className="text-xs text-amber-700 dark:text-amber-300">
                        degraded — automatic fires paused
                      </div>
                    )}
                  </td>
                  <td className="pr-3">
                    <Toggle on={!!row.enabled}
                      label={`Enable ${row.id}`}
                      onClick={() => setRow(i, { enabled: !row.enabled })} />
                  </td>
                  <td className="pr-3">
                    <Input type="number" className="w-20" min={1}
                      aria-label={`${row.id} interval minutes`}
                      value={row.interval_minutes ?? 60}
                      onChange={(e) => setRow(i, {
                        interval_minutes: Number(e.target.value) || 1,
                      })} />
                    <span className="ml-1 text-xs text-neutral-500">min</span>
                  </td>
                  <td className="pr-3">
                    {row.reserved ? (
                      <span className="text-xs text-neutral-500">
                        every Curator board
                      </span>
                    ) : (
                      <Select className="w-36" value={row.pmo || ""}
                        aria-label={`${row.id} target board`}
                        onChange={(e) => setRow(i, { pmo: e.target.value })}>
                        <option value="">—</option>
                        {(cfg.pmos || []).map((p) => (
                          <option key={p.name} value={p.name}>{p.name}</option>
                        ))}
                      </Select>
                    )}
                  </td>
                  <td className="pr-3">
                    {row.reserved ? (
                      <span className="font-mono text-xs">EXECUTE</span>
                    ) : (
                      <Select className="w-32" value={row.entry_stage || "EXECUTE"}
                        aria-label={`${row.id} entry stage`}
                        onChange={(e) => setRow(i, { entry_stage: e.target.value })}>
                        {STAGES.map((s) => (
                          <option key={s} value={s}>{s}</option>
                        ))}
                      </Select>
                    )}
                  </td>
                  <td className="text-right">
                    <div className="flex justify-end gap-1">
                      <ImmediateBadge text="Run now is instant" />
                      <Button kind="ghost" icon={Play}
                        title={row.reserved
                          ? "Fire Memory Curator now — skips empty-queue only on automatic fires"
                          : "Create a ticket now"}
                        onClick={() => runNow(row.id)}>
                        Run now
                      </Button>
                      {!row.reserved && (
                        <Button kind="danger-ghost" icon={Trash2}
                          onClick={() => removeRow(i)}>
                          Delete
                        </Button>
                      )}
                    </div>
                    {runMsg[row.id] && (
                      <p className="mt-1 text-xs text-neutral-500">{runMsg[row.id]}</p>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="mt-4 divide-y divide-neutral-100 dark:divide-neutral-800">
          {rows.map((row, i) => (
            <SettingRow key={`tpl-${row.id}-${i}`}
              label={`${row.name || row.id} ticket text`}
              desc="Becomes the ticket description. {timestamp} is interpolated; no other substitutions.">
              <Textarea className="min-h-24 w-full max-w-xl text-sm"
                aria-label={`${row.id} description template`}
                value={row.description_template || ""}
                onChange={(e) => setRow(i, {
                  description_template: e.target.value,
                })} />
            </SettingRow>
          ))}
        </div>
      </Section>
      <ConfirmDialog open={!!confirm} title={confirm?.title} body={confirm?.body}
        confirmLabel={confirm?.confirmLabel} confirmKind="danger"
        onCancel={() => setConfirm(null)}
        onConfirm={confirm?.action} />
    </>
  );
}
