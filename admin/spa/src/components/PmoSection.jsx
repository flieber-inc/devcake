import React, { useEffect, useState } from "react";
import { get, send } from "../api.js";
import { Section } from "./Card.jsx";
import { Field, SecretField, Input } from "./Field.jsx";
import SettingRow from "./SettingRow.jsx";
import Button from "./Button.jsx";
import Toggle from "./Toggle.jsx";
import { ConfirmDialog } from "./Modal.jsx";
import ImmediateBadge from "./ImmediateBadge.jsx";
import InstantZone from "./InstantZone.jsx";
import RepoChips from "./RepoChips.jsx";
import { ADOPTION_COPY } from "../lib/configLabels.js";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";
import { getRegistry, loadRegistry } from "../lib/registry.js";
import { nextFreeName, useNewNames } from "../lib/instanceNames.js";

export default function PmoSection({ newNamesState, health = {}, healthError = false,
                                     onHealthChange }) {
  const { dr } = useSharedDraft();
  const [registry, setRegistry] = useState(getRegistry());
  useEffect(() => { loadRegistry().then(setRegistry); }, []);
  const [confirm, setConfirm] = useState(null); // flip-time danger + delete confirms
  // per-PMO intake: App-owned /health (like the sidebar master), never the
  // config draft — Discard must not desync a safety switch from the server.
  const [intakeOverride, setIntakeOverride] = useState({}); // name → bool optimistic
  const [intakeBusy, setIntakeBusy] = useState({});
  const [intakeErr, setIntakeErr] = useState({});
  // repos WITHOUT a stored Access (write) token cannot join a PMO's WORK
  // set (founder request 2026-07-15) — EXECUTE would fail at push; they
  // remain selectable as reference repos
  const [repoHasToken, setRepoHasToken] = useState({});
  const repoNamesKey = (dr.draft?.cfg.repos || []).map((r) => r.name).join(",");
  useEffect(() => {
    const names = repoNamesKey ? repoNamesKey.split(",").filter(Boolean) : [];
    if (!names.length) { setRepoHasToken({}); return; }
    const q = names.map((n) => `repo:${n}:token`).join(",");
    get(`/secrets-check?conn=${encodeURIComponent(q)}`)
      .then((r) => setRepoHasToken(Object.fromEntries(
        names.map((n) => [n, !!r.conn[`repo:${n}:token`]?.present]))))
      .catch(() => setRepoHasToken({}));
  }, [repoNamesKey]);
  const [testResult, setTestResult] = useState({});
  // PMO cards added/renamed this session stay name-editable even when their
  // name collides with a still-saved one (delete-then-re-add / mid-typing trap)
  // the Set is owned by the dispatcher (survives section switches — D5 #12)
  const newPmoNames = useNewNames(dr.server?.cfg.pmos, dr.draft?.cfg.pmos,
                                  newNamesState);

  const cfg = dr.draft.cfg;
  const setField = dr.setField;
  // stored secrets are keyed by instance name — renaming a saved instance
  // would orphan them, so the name locks once saved (remove + re-add to
  // rename; removal also deletes the instance's stored secrets). A card
  // counts as saved only when the server holds its name AND it wasn't
  // (re)added this session, so a new card can never be born frozen.
  const savedPmoNames = new Set((dr.server.cfg.pmos || []).map((p) => p.name));
  // only the LAST card carrying a name counts as the session-added one — a
  // new card duplicating a saved name never unlocks the saved card itself
  const pmoNameLocked = (name, idx) =>
    savedPmoNames.has(name) &&
    !(newPmoNames.has(name) && idx === cfg.pmos.map((p) => p.name).lastIndexOf(name));

  // flip-time danger confirms (founder decision): the scary dialog interrupts
  // at flip time, but confirming only writes the DRAFT — nothing persists
  // until the page-level Save (whose review re-highlights these rows).
  const guardedFlip = (path, value, title, body) =>
    setConfirm({
      title, body, confirmLabel: "I understand — proceed",
      action: () => { setField(path, value); setConfirm(null); },
    });

  const test = async (kind) =>
    setTestResult({ ...testResult, [kind]: await send("POST", `/connections/${kind}/test`) });
  // per-instance tests (schema v3 / M10): keyed pmo:{name} / forge:{name}
  const testPmo = async (name) =>
    setTestResult({ ...testResult,
                    [`pmo:${name}`]: await send("POST", `/connections/pmo/${name}/test`) });

  // Narrow endpoint — never rewrites the pmos list (lost-update / secret-delete race).
  // State comes from App /health; only saved instances can toggle live.
  // Refuse while health is unknown (same contract as the sidebar master).
  const healthKnown = !healthError && health.pmo_instances != null;
  const togglePmoIntake = async (name, idx) => {
    if (intakeBusy[name] || !name || !savedPmoNames.has(name)) return;
    if (!healthKnown || dr.errors[`cfg.pmos.${idx}.name`]) return;
    const healthPaused = !!(health.pmo_instances || {})[name]?.intake_paused;
    const current = intakeOverride[name] ?? healthPaused;
    const next = !current;
    setIntakeOverride((o) => ({ ...o, [name]: next }));
    setIntakeBusy((b) => ({ ...b, [name]: true }));
    setIntakeErr((e) => ({ ...e, [name]: "" }));
    try {
      await send("PUT", `/config/pmos/${encodeURIComponent(name)}/intake`,
        { paused: next });
      onHealthChange?.((h) => ({
        ...h,
        pmo_instances: {
          ...(h.pmo_instances || {}),
          [name]: { ...(h.pmo_instances || {})[name], intake_paused: next },
        },
      }));
    } catch (e) {
      setIntakeErr((er) => ({
        ...er, [name]: `✗ ${String(e.message || e)}`,
      }));
      setTimeout(() => setIntakeErr((er) => ({ ...er, [name]: "" })), 5000);
    } finally {
      setIntakeOverride((o) => {
        const n = { ...o };
        delete n[name];
        return n;
      });
      setIntakeBusy((b) => ({ ...b, [name]: false }));
    }
  };

  return (
    <>
      <Section id="pmo" title="PMO connections"
        description="The PMO teams DevCake watches, and how missions are adopted."
        help={`One instance per team. Supported: ${registry.pmo_systems.map((s) => s.display_name).join(", ")}. Instance names prefix branches and run ids (LINEAR-DEV-17).`}>
        {cfg.pmos.map((inst, idx) => {
          const tr = testResult[`pmo:${inst.name}`];
          const sysMeta = (registry.pmo_systems || []).find((s) => s.id === inst.system)
            || { needs_api_base: false, team_key_label: "Team key",
                 team_key_help: "", api_base_help: "" };
          const saved = savedPmoNames.has(inst.name);
          const nameBad = !!dr.errors[`cfg.pmos.${idx}.name`];
          const healthPaused = !!(health.pmo_instances || {})[inst.name]?.intake_paused;
          const paused = intakeOverride[inst.name] ?? healthPaused;
          const masterOff = !!health.intake_paused;
          const label = !saved
            ? "Mission intake — save instance first"
            : masterOff
              ? (paused ? "Mission intake — PAUSED (master also OFF)" : "Mission intake — ON (master paused)")
              : (paused ? "Mission intake — PAUSED" : "Mission intake — ON");
          return (
            <div key={idx} className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-mono text-sm font-semibold">{inst.name || "(unnamed)"}</span>
                {cfg.pmos.length > 1 && (
                  <Button kind="danger-ghost" onClick={() => {
                    const doRemove = () => {
                      newPmoNames.untrack(inst.name);
                      setField("cfg.pmos", cfg.pmos.filter((_, i) => i !== idx));
                    };
                    if (savedPmoNames.has(inst.name)) {
                      // saving the removal permanently deletes the stored
                      // secret — worth an explicit confirm (audit A21)
                      setConfirm({
                        title: `Remove PMO instance "${inst.name}"?`,
                        body: "Removing it and saving permanently deletes its stored API key; in-flight runs of this instance fail cleanly. Nothing changes until you Save.",
                        confirmLabel: "Remove from draft",
                        action: () => { doRemove(); setConfirm(null); },
                      });
                    } else doRemove();
                  }}>
                    Remove
                  </Button>
                )}
              </div>
              {saved ? (
                <InstantZone note="applies immediately — does not wait for Save">
                  <SettingRow
                    label={label}
                    desc={paused
                      ? "This PMO dispatches no new runs. In-flight work still finishes."
                      : masterOff
                        ? "This PMO is open, but the sidebar master switch freezes every team."
                        : "This PMO may dispatch new runs."}
                    help="Per-team intake under the sidebar Mission intake master switch. Master OFF freezes every PMO; this switch freezes only this instance.">
                    <Toggle
                      on={!paused}
                      label={`Mission intake for ${inst.name}`}
                      disabled={!!intakeBusy[inst.name] || nameBad || !healthKnown}
                      onClick={() => togglePmoIntake(inst.name, idx)} />
                  </SettingRow>
                  {intakeErr[inst.name] && (
                    <p className="text-xs text-red-600 dark:text-red-400">
                      {intakeErr[inst.name]}
                    </p>
                  )}
                </InstantZone>
              ) : (
                <p className="text-xs text-neutral-500 dark:text-neutral-400">
                  Mission intake for this card unlocks after you Save the new instance.
                </p>
              )}
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <Field label="Instance name"
                  help="Operator-chosen identity (lowercase letters/digits, ≤12, no hyphens). Uppercased, it prefixes this instance's branches and run ids. Locked once saved — stored secrets and in-flight missions key on it; remove and re-add to rename.">
                  <Input value={inst.name} disabled={pmoNameLocked(inst.name, idx)}
                  onChange={(e) => {
                    newPmoNames.rename(inst.name, e.target.value);
                    setField(`cfg.pmos.${idx}.name`, e.target.value);
                  }} />
                  {dr.errors[`cfg.pmos.${idx}.name`] && (
                    <span className="mt-1 block text-xs text-red-600 dark:text-red-400">
                      ✗ {dr.errors[`cfg.pmos.${idx}.name`]}
                    </span>
                  )}</Field>
                <Field label="System"
                  help="PMO product this instance talks to. Driven by the adapter registry — adding an adapter does not require SPA edits.">
                  <select
                    className="w-full rounded-md border border-neutral-300 bg-white px-2 py-1.5 text-sm dark:border-neutral-700 dark:bg-neutral-950"
                    value={inst.system || "linear"}
                    onChange={(e) => setField(`cfg.pmos.${idx}.system`, e.target.value)}
                    aria-label="PMO system"
                  >
                    {(registry.pmo_systems || []).map((s) => (
                      <option key={s.id} value={s.id}>{s.display_name}</option>
                    ))}
                  </select>
                </Field>
                <Field label={sysMeta.team_key_label || "Team key"}
                  help={sysMeta.team_key_help || ""}>
                  <Input value={inst.team_key}
                  onChange={(e) => setField(`cfg.pmos.${idx}.team_key`, e.target.value)} /></Field>
                {sysMeta.needs_api_base && (
                  <Field label="API base"
                    help={sysMeta.api_base_help || "Origin of the PMO API reachable from the app container."}>
                    <Input value={inst.api_base || ""}
                      placeholder="http://gitea:3000"
                      onChange={(e) => setField(`cfg.pmos.${idx}.api_base`,
                        e.target.value.trim() || null)} />
                  </Field>
                )}
                <SecretField label="API key"
                  help="This instance's PMO API key. Stored securely on the app volume — never echoed back, never in .env."
                  refKey={`pmo:${inst.name}:api_key`} paste
                  locked={!pmoNameLocked(inst.name, idx)} />
                <RepoChips label="Repositories"
                  help="The ORDERED set of repos this instance's missions may target — only repos with a stored Access token qualify (work needs push). Click to toggle; the first selected is the default for missions without a `devcake-repo:` marker; markers must name a listed repo. Empty = every mission gets its own internal-forge repo."
                  all={cfg.repos} selected={inst.repos || []}
                  excluded={inst.reference_repos || []}
                  excludedNote="reference repo"
                  unavailable={cfg.repos.map((r) => r.name).filter((n) => !repoHasToken[n])}
                  unavailableNote="no Access token stored — usable only as a reference repo"
                  firstBadge=" · default"
                  onChange={(next) => setField(`cfg.pmos.${idx}.repos`, next)} />
                <RepoChips label="Reference repos"
                  help="Read-only consultation material (docs sources, style guides) cloned into EVERY stage's workspace alongside the mission's repository. Never a work target — markers naming one gate. Multiple supported."
                  all={cfg.repos} selected={inst.reference_repos || []}
                  excluded={inst.repos || []}
                  excludedNote="work repo"
                  onChange={(next) => setField(`cfg.pmos.${idx}.reference_repos`, next)} />
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <Button kind="ghost" onClick={() => testPmo(inst.name)}>Test connection</Button>
                <ImmediateBadge text="tests saved values" />
                {tr && (
                  <span className={`text-sm ${tr.ok ? "text-green-700 dark:text-green-400" : "text-red-600"}`}>
                    {tr.ok
                      ? `✓ team ${tr.team}: ${tr.labels}/${tr.labels_expected ?? 10} labels, ${tr.missions_visible} items visible`
                      : `✗ ${tr.error}`}
                  </span>
                )}
              </div>
            </div>
          );
        })}
        <Button kind="ghost" onClick={() => {
          const name = nextFreeName("linear", cfg.pmos, dr.server.cfg.pmos);
          newPmoNames.track(name);
          const defaultSystem = (registry.pmo_systems || [])[0]?.id || "linear";
          setField("cfg.pmos", [...cfg.pmos,
            { name, system: defaultSystem,
              team_key: "", api_base: null, repos: [],
              reference_repos: [] }]);
        }}>
          + Add PMO instance
        </Button>
        <div className="divide-y divide-neutral-100 border-t border-neutral-100 dark:divide-neutral-800 dark:border-neutral-800">
          <SettingRow label="Poll interval"
            desc="Seconds between polls of each PMO for new or changed missions."
            help="Lower = faster pickup, more API calls.">
            <Input type="number" className="w-24" value={cfg.poll_interval_seconds}
              aria-label="Poll interval (seconds)"
              onChange={(e) => setField("cfg.poll_interval_seconds", Number(e.target.value))} />
          </SettingRow>
          <SettingRow label="Adoption mode"
            desc={cfg.adoption_mode === "opt_in"
              ? "opt-in — only items you label DEVCAKE are adopted."
              : "opt-out — every non-completed item in the team, backlog included."}
            help="opt-in: DevCake only adopts items you label DEVCAKE. opt-out: it adopts every non-completed issue and project in the team, including the backlog.">
            <span className={`text-xs ${cfg.adoption_mode === "opt_in" ? "font-semibold" : "text-neutral-500 dark:text-neutral-400"}`}>
              opt-in
            </span>
            <Toggle on={cfg.adoption_mode === "opt_out"} label="Adoption mode"
              onClick={() =>
                cfg.adoption_mode === "opt_in"
                  ? guardedFlip("cfg.adoption_mode", "opt_out", "Adopt the ENTIRE team?",
                      ADOPTION_COPY + "\n\n(Drafted now; applies when you Save.)")
                  : setField("cfg.adoption_mode", "opt_in")} />
            <span className={`text-xs ${cfg.adoption_mode === "opt_out" ? "font-semibold" : "text-neutral-500 dark:text-neutral-400"}`}>
              opt-out
            </span>
          </SettingRow>
        </div>
      </Section>

      <ConfirmDialog open={!!confirm} {...(confirm || {})}
        onConfirm={() => confirm.action()}
        onCancel={() => setConfirm(null)} />
    </>
  );
}
