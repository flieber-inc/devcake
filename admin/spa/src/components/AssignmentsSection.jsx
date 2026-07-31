import React from "react";
import { Section } from "./Card.jsx";
import { Help, Input, Select } from "./Field.jsx";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";

const MISSION_TYPES = ["ONBOARD", "PLAN", "EXECUTE", "REVIEW"];

export default function AssignmentsSection() {
  const { dr } = useSharedDraft();
  const setField = dr.setField;
  const pmos = dr.draft.cfg.pmos || [];

  // harness-mismatch advisory for a global assignment row (replaces the old
  // blocking dialog and its cancelAction special case)
  const argsAdvisory = (mt) => {
    const a = dr.draft.assignments[mt] || {};
    const sa = dr.server.assignments[mt] || {};
    if (!a.extra_cli_args || a.dev_type === sa.dev_type) return null;
    const oldH = (dr.server.devTypes[sa.dev_type] || dr.draft.devTypes[sa.dev_type])?.harness_template;
    const newH = dr.draft.devTypes[a.dev_type]?.harness_template;
    if (!oldH || !newH || oldH === newH) return null;
    return { oldH, newH, newDt: a.dev_type };
  };

  // same advisory for an instance override row (ADR-0019)
  const ovAdvisory = (i, mt) => {
    const a = dr.draft.cfg.pmos[i]?.assignments?.[mt];
    const sa = dr.server.cfg.pmos[i]?.assignments?.[mt];
    if (!a?.extra_cli_args || !sa || a.dev_type === sa.dev_type) return null;
    const oldH = (dr.server.devTypes[sa.dev_type] || dr.draft.devTypes[sa.dev_type])?.harness_template;
    const newH = dr.draft.devTypes[a.dev_type]?.harness_template;
    if (!oldH || !newH || oldH === newH) return null;
    return { oldH, newH, newDt: a.dev_type };
  };

  // ADR-0019: the row that actually staffs `mt` on instance `p` — its
  // override when present, else the global row
  const effectiveDev = (p, mt) =>
    p?.assignments?.[mt]?.dev_type || dr.draft.assignments[mt]?.dev_type || "";

  const setOverride = (i, p, mt, devType) => {
    if (!devType) {
      // back to inherit: the row leaves the map (presence = override)
      const { [mt]: _drop, ...rest } = p.assignments || {};
      setField(`cfg.pmos.${i}.assignments`, rest);
    } else if (p.assignments?.[mt]) {
      setField(`cfg.pmos.${i}.assignments.${mt}.dev_type`, devType);
    } else {
      // fresh override starts with EMPTY args — flags are harness-specific
      // and never inherited from the global row
      setField(`cfg.pmos.${i}.assignments.${mt}`,
               { dev_type: devType, extra_cli_args: "" });
    }
  };

  // Performance tip only — not a security control (reviewer token is).
  const sharedReviewBanner = (label, ex, rev) =>
    ex && rev && ex === rev && (
      <div className="mb-3 rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2 text-sm text-neutral-700 dark:border-neutral-700 dark:bg-neutral-900/40 dark:text-neutral-300">
        {label}EXECUTE and REVIEW share the same Dev Type. Assigning a different type
        for REVIEW is optional — useful so REVIEW can carry review-focused skills and
        its own identifying prompt. Not a security control; formal forge approval uses
        the app-only reviewer token.
      </div>
    );

  return (
      <Section id="assignments" title="Assignments"
        description={pmos.length
          ? "Which Dev Type handles each mission type. Per-PMO overrides below replace a row for that instance only."
          : "Which Dev Type handles each mission type."}>
        {sharedReviewBanner("", dr.draft.assignments?.EXECUTE?.dev_type,
                            dr.draft.assignments?.REVIEW?.dev_type)}
        <div className="overflow-x-auto">
          <table className="w-full min-w-[32rem] text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
              <tr><th className="py-1">Mission type</th><th>Dev type</th>
                <th>Extra CLI args (harness-specific)
                  <Help text="Appended to the harness CLI for this mission type, e.g. --max-turns 15. Flags are harness-specific — they rarely survive a dev type change." /></th></tr>
            </thead>
            <tbody>
              {MISSION_TYPES.map((mt) => {
                const adv = argsAdvisory(mt);
                return (
                  <tr key={mt} className="border-t border-neutral-100 dark:border-neutral-800">
                    <td className="py-2 font-mono text-xs font-semibold">{mt}</td>
                    <td className="py-2 pr-3">
                      <Select value={dr.draft.assignments[mt]?.dev_type || ""}
                        onChange={(e) => setField(`assignments.${mt}.dev_type`, e.target.value)}>
                        {dr.order.map((n) => <option key={n}>{n}</option>)}
                      </Select>
                    </td>
                    <td className="py-2">
                      <Input value={dr.draft.assignments[mt]?.extra_cli_args || ""}
                        placeholder="e.g. --max-turns 15"
                        onChange={(e) => setField(`assignments.${mt}.extra_cli_args`, e.target.value)} />
                      {adv && (
                        <p className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400">
                          These args were written for the {adv.oldH} harness; {adv.newDt} uses{" "}
                          {adv.newH} — flags rarely transfer.
                          <button
                            onClick={() => setField(`assignments.${mt}.extra_cli_args`, "")}
                            className="rounded border border-amber-300 px-1.5 py-0.5 font-medium hover:bg-amber-100 dark:border-amber-700 dark:hover:bg-amber-950">
                            clear args
                          </button>
                        </p>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {pmos.map((p, i) => (
          <div key={p.name || i} className="mt-5">
            <h4 className="mb-1 text-sm font-semibold">
              Overrides — <span className="font-mono">{p.name || `PMO #${i + 1}`}</span>
              <Help text="A row set here replaces the global assignment for this PMO instance only — Dev Type and CLI args together, never mixed. Inherit rows follow the global table above (live: a later global edit applies here too)." />
            </h4>
            {(p.assignments?.EXECUTE?.dev_type || p.assignments?.REVIEW?.dev_type)
              ? sharedReviewBanner(`On ${p.name || `PMO #${i + 1}`}: `,
                                   effectiveDev(p, "EXECUTE"), effectiveDev(p, "REVIEW"))
              : null /* no override in play — the global banner already covers it */}
            <div className="overflow-x-auto">
              <table className="w-full min-w-[32rem] text-sm">
                <tbody>
                  {MISSION_TYPES.map((mt) => {
                    const row = p.assignments?.[mt];
                    const err = dr.errors[`cfg.pmos.${i}.assignments.${mt}.dev_type`];
                    const adv = ovAdvisory(i, mt);
                    return (
                      <tr key={mt} className="border-t border-neutral-100 dark:border-neutral-800">
                        <td className="w-32 py-2 font-mono text-xs font-semibold">{mt}</td>
                        <td className="py-2 pr-3">
                          <Select value={row?.dev_type || ""}
                            onChange={(e) => setOverride(i, p, mt, e.target.value)}>
                            <option value="">
                              inherit — {dr.draft.assignments[mt]?.dev_type || "(unassigned)"}
                            </option>
                            {dr.order.map((n) => <option key={n}>{n}</option>)}
                          </Select>
                          {err && (
                            <p className="mt-1 text-xs text-red-600 dark:text-red-400">✗ {err}</p>
                          )}
                        </td>
                        <td className="py-2">
                          {row?.dev_type ? (
                            <>
                              <Input value={row.extra_cli_args || ""}
                                placeholder="e.g. --max-turns 15"
                                onChange={(e) => setField(
                                  `cfg.pmos.${i}.assignments.${mt}.extra_cli_args`, e.target.value)} />
                              {adv && (
                                <p className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400">
                                  These args were written for the {adv.oldH} harness; {adv.newDt} uses{" "}
                                  {adv.newH} — flags rarely transfer.
                                  <button
                                    onClick={() => setField(`cfg.pmos.${i}.assignments.${mt}.extra_cli_args`, "")}
                                    className="rounded border border-amber-300 px-1.5 py-0.5 font-medium hover:bg-amber-100 dark:border-amber-700 dark:hover:bg-amber-950">
                                    clear args
                                  </button>
                                </p>
                              )}
                            </>
                          ) : (
                            <span className="text-xs text-neutral-400 dark:text-neutral-500">
                              inherits global args
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        ))}
      </Section>
  );
}
