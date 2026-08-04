import React from "react";
import { Section } from "./Card.jsx";
import { Input, Select } from "./Field.jsx";
import SettingRow from "./SettingRow.jsx";
import Toggle from "./Toggle.jsx";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";

const CONTINUATION_POLICIES = ["auto", "resume-only", "fresh-only", "off"];

const ATTEMPT_RESET_POLICIES = ["label-ops", "any-comment", "unlimited"];

const ATTEMPT_RESET_DESC = {
  "label-ops":
    "Strict — ordinary comments don't grant fresh attempts; a comment with DEVCAKE-RETRY does.",
  "any-comment":
    "Any human comment resets the step's attempt count (pre-2026-08 behavior).",
  unlimited:
    "Never gives up — DevCake retries failed steps indefinitely and warns about cost.",
};

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
          <SettingRow label="Attempt reset"
            desc={ATTEMPT_RESET_DESC[cfg.attempt_reset] || `${cfg.attempt_reset} (set via API)`}
            help="A DevCake idiosyncrasy worth understanding: each pipeline step retries up to the attempt limit, and certain events grant FRESH attempts — the count restarts. Removing DEVCAKE-FAILED and a later step finishing always reset. What else resets is this policy. Strict (default): only a comment containing the literal DEVCAKE-RETRY — the deliberate human gesture. Any comment: every non-DevCake comment resets, which reads naturally ('a human intervened') but lets a chatty integration (a Linear↔GitHub sync bot, a CI notifier) keep the counter at 1 forever — the mission then never fails AND never stops, retrying at token cost indefinitely. Unlimited: DevCake never applies DEVCAKE-FAILED at all — an explicit choice for self-hosted models where retries cost watts, not dollars; a cumulative-cost warning posts to the mission feed at the review-loop cadence so the mode stays loud. DEVCAKE-SKIP always stops a mission regardless of policy.">
            <Select className="w-44" value={cfg.attempt_reset}
              aria-label="Attempt reset policy"
              onChange={(e) => setField("cfg.attempt_reset", e.target.value)}>
              {!ATTEMPT_RESET_POLICIES.includes(cfg.attempt_reset) && (
                // an API/YAML-set policy outside the offered values must
                // round-trip — a controlled select with no matching option
                // would misrender it and the next save would clobber it
                <option value={cfg.attempt_reset}>
                  {cfg.attempt_reset} (set via API)
                </option>
              )}
              <option value="label-ops">Strict (DEVCAKE-RETRY / labels)</option>
              <option value="any-comment">Any comment</option>
              <option value="unlimited">Unlimited (never give up)</option>
            </Select>
          </SettingRow>
          <SettingRow label="Brake on missing results"
            desc={cfg.brake_on_bad_output
              ? "ON — repeated no-result runs (exit 11) count as backend-brake evidence."
              : "OFF — only harness faults (exit 15) engage the backend brake (default)."}
            help="Another nuance: when a model backend degrades fleet-wide, every container may fail the same way at once. DevCake's backend brake correlates such failures — the same failure class across two or more missions in the recent window — and responds by excusing those attempts (they don't count toward the limit) and throttling the Dev Type to a single probe run until two clean runs clear it. By default only harness faults (exit 15: the CLI reported an API/backend error) are brake evidence. This switch adds exit 11 — the run ended without writing its result file — which is also the signature of a backend returning garbage to every container at once. It stays off by default because the continuation loop already recovers most solitary no-result runs, and a genuinely confused model should burn its attempts honestly. Note: at a per-type concurrency of 1 the throttle arm changes nothing — only the attempt-excusal arm acts.">
            <Toggle on={!!cfg.brake_on_bad_output}
              label="Brake on missing results"
              onClick={() => setField("cfg.brake_on_bad_output",
                !cfg.brake_on_bad_output)} />
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
          <SettingRow label="Mirror sync max age"
            desc={Number(cfg.repo_mirror?.sync_max_age_seconds) === 0
              ? "0 — mirrors sync before every dispatch."
              : `Mirrors synced within ${cfg.repo_mirror?.sync_max_age_seconds}s count as fresh.`}
            help="Every configured repository is served to Devs from an app-maintained mirror (mandatory — there is no off switch). A successful sync is a fail-closed precondition: a mission whose mirrors cannot be freshened does not dispatch that cycle and retries on the next poll. 0 (default) syncs before every dispatch; a higher value reduces forge requests between rapid-fire mission steps at the cost of bounded staleness.">
            <Input type="number" className="w-24" min={0}
              value={cfg.repo_mirror?.sync_max_age_seconds ?? 0}
              aria-label="Mirror sync max age (seconds)"
              onChange={(e) => setField("cfg.repo_mirror.sync_max_age_seconds", Number(e.target.value))} />
          </SettingRow>
          <SettingRow label="Mirror LFS content"
            desc={cfg.repo_mirror?.lfs
              ? "ON — Devs receive real LFS files from the mirror."
              : "OFF — LFS pointers ride as-is (status quo)."}
            help="With this on, mirror syncs also fetch Git LFS content (default-branch scope) so Devs get real files instead of pointer files — at the cost of mirror disk and initial download. Off keeps exactly the previous behavior: LFS repos clone with pointer files.">
            <Toggle on={!!cfg.repo_mirror?.lfs}
              label="Mirror LFS content"
              onClick={() => setField("cfg.repo_mirror.lfs", !cfg.repo_mirror?.lfs)} />
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
