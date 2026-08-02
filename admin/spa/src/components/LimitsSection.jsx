import React from "react";
import { Section } from "./Card.jsx";
import { Input, Select } from "./Field.jsx";
import SettingRow from "./SettingRow.jsx";
import Toggle from "./Toggle.jsx";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";

const CONTINUATION_POLICIES = ["auto", "resume-only", "fresh-only", "off"];

export default function LimitsSection() {
  const { dr } = useSharedDraft();
  const cfg = dr.draft.cfg;
  const setField = dr.setField;

  return (
      <Section id="limits" title="Limits"
        description="Global concurrency and safety ceilings.">
        <div className="divide-y divide-neutral-100 dark:divide-neutral-800">
          <SettingRow label="Global max Devs"
            desc="Effective ceiling = min(global, Σ per-type caps)."
            help="Primary host-protection control. Dagu 2.10.5 cannot apply Docker CPU/memory/PID limits to Dev containers — this cap is the effective throttle; hard per-container limits are planned.">
            <Input type="number" className="w-24" value={cfg.concurrency.global_max}
              aria-label="Global max Devs"
              onChange={(e) => setField("cfg.concurrency.global_max", Number(e.target.value))} />
          </SettingRow>
          <SettingRow label="Dev run timeout"
            desc="Wall-clock limit per Dev run, in minutes."
            help="Applies while dispatched/running only — finalizing is never timeout-killed. A timed-out mission is retried up to its attempt limit.">
            <Input type="number" className="w-24" value={cfg.dev_timeout_minutes}
              aria-label="Dev run timeout (minutes)"
              onChange={(e) => setField("cfg.dev_timeout_minutes", Number(e.target.value))} />
          </SettingRow>
          <SettingRow label="Review-loop warning"
            desc="Warn after every N rejections of EXECUTE's work."
            help="When REVIEW keeps rejecting EXECUTE's work, DevCake posts a warning to the mission's activity feed every N rejections so you can intervene. Must be ≥ 1.">
            <Input type="number" className="w-24" min={1} value={cfg.review_loop_warning_every}
              aria-label="Review-loop warning every N rejections"
              onChange={(e) => setField("cfg.review_loop_warning_every", Number(e.target.value))} />
          </SettingRow>
          <SettingRow label="Accept misplaced result files"
            desc={cfg.recover_misplaced_result
              ? "ON — a result file written elsewhere in the workspace still counts."
              : "OFF — only /workspace/out/result.json counts."}
            help="Devs are told to write /workspace/out/result.json. With this on, DevCake also accepts a result file a Dev wrote somewhere else in its workspace — but only if the file was created during that run and passes the same validation. Either way, the misplacement is always recorded in the run terminal and the mission feed, so you can fix the prompt.">
            <Toggle on={!!cfg.recover_misplaced_result}
              label="Accept misplaced result files"
              onClick={() => setField("cfg.recover_misplaced_result",
                !cfg.recover_misplaced_result)} />
          </SettingRow>
          <SettingRow label="Continuation policy"
            desc="Recover a run that ended cleanly without writing its result file."
            help="When a harness exits cleanly but never wrote result.json (a weak model ending its turn mid-mission), DevCake relaunches the harness inside the same container with a reminder instead of failing the attempt. Auto resumes the same session where the harness supports it and switches permanently to a fresh session after a zero-progress continuation. Resume-only never falls back to fresh (fails as before when resume is unavailable); fresh-only always starts a new session in the same workspace; off disables the loop. Plan runs never continue.">
            <Select className="w-40" value={cfg.continuation_policy}
              aria-label="Continuation policy"
              onChange={(e) => setField("cfg.continuation_policy", e.target.value)}>
              {!CONTINUATION_POLICIES.includes(cfg.continuation_policy) && (
                // an API/YAML-set policy outside the offered values must
                // round-trip — a controlled select with no matching option
                // would misrender it and the next save would clobber it
                <option value={cfg.continuation_policy}>
                  {cfg.continuation_policy} (set via API)
                </option>
              )}
              <option value="auto">Auto (resume, then fresh)</option>
              <option value="resume-only">Resume only</option>
              <option value="fresh-only">Fresh only</option>
              <option value="off">Off</option>
            </Select>
          </SettingRow>
          <SettingRow label="Max continuations per run"
            desc={Number(cfg.max_continuations) === 0
              ? "0 — continuation is off."
              : `Up to ${cfg.max_continuations} relaunches before the run fails.`}
            help="The budget is the ONLY terminator: a stalled continuation escalates auto from resume to fresh but never ends the run early, so large experimental budgets (10, 50) run to completion — bounded only by the Dev run timeout. Each relaunch resets the harness's own --max-turns counter, so the effective turn budget is (continuations + 1) × max-turns. 0 disables the loop.">
            <Input type="number" className="w-24" min={0} value={cfg.max_continuations}
              aria-label="Max continuations per run"
              onChange={(e) => setField("cfg.max_continuations", Number(e.target.value))} />
          </SettingRow>
          <SettingRow label="Service auto-restart"
            desc="Long-lived services restart unless stopped (compose-managed)."
            help='Services use restart: unless-stopped in docker-compose.yml. This panel cannot rewrite compose — set restart: "no" in the file to disable.'>
            <span className="text-sm text-neutral-500 dark:text-neutral-400">managed in compose</span>
          </SettingRow>
        </div>
      </Section>
  );
}
