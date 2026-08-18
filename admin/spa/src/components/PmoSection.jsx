import React, { useEffect, useState } from "react";
import { get, send } from "../api.js";
import { Section } from "./Card.jsx";
import { Field, Help, SecretField, Input } from "./Field.jsx";
import SettingRow from "./SettingRow.jsx";
import Button from "./Button.jsx";
import Toggle from "./Toggle.jsx";
import { ConfirmDialog } from "./Modal.jsx";
import ImmediateBadge from "./ImmediateBadge.jsx";
import InstantZone from "./InstantZone.jsx";
import MoreMenu from "./MoreMenu.jsx";
import RepoChips from "./RepoChips.jsx";
import ClearSecretsDialog, { CLEAR_SECRETS_ENTRY } from "./ClearSecretsDialog.jsx";
import { ADOPTION_COPY } from "../lib/configLabels.js";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";
import { getRegistry, loadRegistry } from "../lib/registry.js";
import { nextFreeName, useNewNames } from "../lib/instanceNames.js";
import { newPmoCard } from "../lib/cards.js";

export default function PmoSection({ newNamesState, health = {}, healthError = false,
                                     onHealthChange }) {
  const { dr, reload } = useSharedDraft();
  const [registry, setRegistry] = useState(getRegistry());
  useEffect(() => { loadRegistry().then(setRegistry); }, []);
  const [confirm, setConfirm] = useState(null); // flip-time danger + delete confirms
  const [clearSecrets, setClearSecrets] = useState(false);
  const [secretsEpoch, setSecretsEpoch] = useState(0);
  const [clearReloadErr, setClearReloadErr] = useState("");
  // per-PMO intake: App-owned /health (like the sidebar master), never the
  // config draft — Discard must not desync a safety switch from the server.
  const [intakeOverride, setIntakeOverride] = useState({}); // name → bool optimistic
  const [intakeBusy, setIntakeBusy] = useState({});
  const [intakeErr, setIntakeErr] = useState({});
  // 2026-08 reviewer round: collapsed summary rows + a name filter past 5
  // instances — the ReposPage pattern (index-keyed expansion so a rename
  // mid-edit never collapses the card being typed in; ≤3 start expanded)
  const [pmoFilter, setPmoFilter] = useState("");
  const [expandedPmos, setExpandedPmos] = useState(() => new Set(
    (dr.draft?.cfg.pmos || []).length <= 3
      ? (dr.draft?.cfg.pmos || []).map((_, i) => i) : []));
  const togglePmoCard = (i) => setExpandedPmos((prev) => {
    const next = new Set(prev);
    if (next.has(i)) next.delete(i); else next.add(i);
    return next;
  });
  // repos WITHOUT a stored Access (write) token cannot join a PMO's WORK
  // set (founder request 2026-07-15) — EXECUTE would fail at push; they
  // remain selectable as reference repos
  const [repoHasToken, setRepoHasToken] = useState({});
  const repoNamesKey = (dr.draft?.cfg.repos || []).map((r) => r.name).join(",");
  useEffect(() => {
    const names = repoNamesKey ? repoNamesKey.split(",").filter(Boolean) : [];
    if (!names.length) { setRepoHasToken({}); return; }
    // CHUNKED (bulk-scale 2026-08-02): one GET for 350 repos built a ~10KB
    // request line, past nginx's 8KB limit. ≤40 names per request keeps
    // URLs short, and a failed chunk marks only ITS repos unknown-truthy
    // (previously any failure disabled EVERY work chip).
    let live = true;
    const chunks = [];
    for (let i = 0; i < names.length; i += 40) chunks.push(names.slice(i, i + 40));
    Promise.all(chunks.map((chunk) => {
      const q = chunk.map((n) => `repo:${n}:token`).join(",");
      return get(`/secrets-check?conn=${encodeURIComponent(q)}`)
        .then((r) => Object.fromEntries(
          chunk.map((n) => [n, !!r.conn[`repo:${n}:token`]?.present])))
        .catch(() => Object.fromEntries(chunk.map((n) => [n, true])));
    })).then((parts) => {
      if (live) setRepoHasToken(Object.assign({}, ...parts));
    });
    return () => { live = false; };
  }, [repoNamesKey, secretsEpoch]);
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
        help={(() => {
          const systems = registry.pmo_systems || [];
          const launch = systems.filter((s) => !s.experimental).map((s) => s.display_name);
          const exp = systems.filter((s) => s.experimental).map((s) => s.display_name);
          const bits = [];
          if (launch.length) bits.push(`Launch-supported: ${launch.join(", ")}`);
          if (exp.length) bits.push(`Experimental (in-tree, not launch-supported): ${exp.join(", ")}`);
          bits.push("Instance names prefix branches and run ids (MYTEAM-DEV-17).");
          return `One instance per team. ${bits.join(". ")}`;
        })()}
        actions={
          <MoreMenu label="More PMO actions" items={[
            { label: CLEAR_SECRETS_ENTRY.menuLabel, danger: true,
              desc: CLEAR_SECRETS_ENTRY.desc,
              onClick: () => setClearSecrets(true) },
          ]} />
        }>
        {clearReloadErr && (
          <p className="text-sm text-red-600 dark:text-red-400">✗ {clearReloadErr}</p>
        )}
        {cfg.pmos.length > 5 && (
          <span className="relative block w-64">
            <Input className="pr-7" value={pmoFilter}
              placeholder={`Filter ${cfg.pmos.length} PMO instances…`}
              aria-label="Filter PMO instances by name"
              onChange={(e) => setPmoFilter(e.target.value)} />
            {pmoFilter && (
              <button type="button" aria-label="Clear PMO filter"
                onClick={() => setPmoFilter("")}
                className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-neutral-500 hover:text-neutral-800 dark:text-neutral-400 dark:hover:text-neutral-100">
                ✕
              </button>
            )}
          </span>
        )}
        {cfg.pmos.map((inst, idx) => {
          const fq = cfg.pmos.length > 5 ? pmoFilter.trim().toLowerCase() : "";
          if (fq && !(inst.name || "").toLowerCase().includes(fq)) return null;
          const tr = testResult[`pmo:${inst.name}`];
          if (!expandedPmos.has(idx)) {
            return (
              <button key={`${idx}-${secretsEpoch}`} type="button"
                data-testid="pmo-summary-row"
                aria-label={`Expand PMO instance ${inst.name}`}
                onClick={() => togglePmoCard(idx)}
                className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 rounded-card border border-neutral-200 px-4 py-2.5 text-left transition hover:bg-stone-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:border-neutral-800 dark:hover:bg-neutral-900">
                <span className="font-mono text-sm font-semibold">{inst.name || "(unnamed)"}</span>
                <span className="text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">{inst.system}</span>
                {inst.managed && (
                  <span className="rounded bg-stone-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
                    Bundled board
                  </span>
                )}
                {inst.team_key && (
                  <span className="text-xs text-neutral-500 dark:text-neutral-400">{inst.team_key}</span>
                )}
                <span className="ml-auto flex shrink-0 items-center gap-3">
                  <span className="text-[11px] text-neutral-400 dark:text-neutral-500">
                    {(inst.repos || []).length} repo{(inst.repos || []).length === 1 ? "" : "s"}
                  </span>
                  {(health.pmo_instances || {})[inst.name]?.intake_paused && (
                    <span className="text-[11px] text-amber-600 dark:text-amber-400">intake paused</span>
                  )}
                  <span aria-hidden className="text-xs text-neutral-400">▸</span>
                </span>
              </button>
            );
          }
          const sysMeta = (registry.pmo_systems || []).find((s) => s.id === inst.system)
            || { needs_api_base: false, team_key_label: "Team key",
                 team_key_help: "", api_base_help: "", operator_note: "" };
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
            <div key={`${idx}-${secretsEpoch}`} className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="flex items-center gap-2">
                  <button type="button"
                    aria-label={`Collapse PMO instance ${inst.name}`}
                    title="Collapse to a summary row"
                    onClick={() => togglePmoCard(idx)}
                    className="rounded text-xs text-neutral-400 hover:text-neutral-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:text-neutral-500 dark:hover:text-neutral-300">
                    ▾
                  </button>
                  <span className="font-mono text-sm font-semibold">{inst.name || "(unnamed)"}</span>
                  {inst.managed && (
                    <span className="rounded bg-stone-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
                      Bundled board
                      <Help text="The auto-provisioned issues board on the bundled Gitea (ADR-0030). Its identity and API key are app-managed and self-healing — a config save that omits it puts it back. Intake, repositories, and staffing stay yours." />
                    </span>
                  )}
                </span>
                {/* the managed board is not removable while the bundled
                    provisioner exists — the next boot would resurrect it and
                    deleting would orphan its app-minted PAT (ADR-0030) */}
                {cfg.pmos.length > 1 && !(inst.managed && health.internal_forge) && (
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
                    className="w-full rounded-md border border-neutral-300 bg-white px-2 py-1.5 text-sm disabled:opacity-60 dark:border-neutral-700 dark:bg-neutral-950"
                    value={inst.system || "linear"}
                    disabled={!!inst.managed}
                    onChange={(e) => setField(`cfg.pmos.${idx}.system`, e.target.value)}
                    aria-label="PMO system"
                  >
                    {(registry.pmo_systems || []).map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.display_name}{s.experimental ? " (experimental)" : ""}
                      </option>
                    ))}
                  </select>
                </Field>
                {sysMeta.operator_note ? (
                  <p className="sm:col-span-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/60 dark:text-amber-300">
                    {sysMeta.operator_note}
                  </p>
                ) : null}
                {(() => {
                  const live = (health.pmo_instances || {})[inst.name] || {};
                  const bits = [];
                  if (live.relations_supported === false) {
                    bits.push("live: relations off — child missions will not block each other");
                  }
                  if (live.attachments_supported === false) {
                    bits.push("live: no official file attachments — feed posts a pointer");
                  }
                  if (!bits.length) return null;
                  return (
                    <p className="sm:col-span-3 text-xs text-amber-800 dark:text-amber-300">
                      {bits.join(". ")}.
                    </p>
                  );
                })()}
                <Field label={sysMeta.team_key_label || "Team key"}
                  help={sysMeta.team_key_help || ""}>
                  <Input value={inst.team_key} disabled={!!inst.managed}
                  onChange={(e) => setField(`cfg.pmos.${idx}.team_key`, e.target.value)} /></Field>
                {sysMeta.needs_api_base && (
                  <Field label="API base"
                    help={sysMeta.api_base_help || "Origin of the PMO API reachable from the app container."}>
                    <Input value={inst.api_base || ""} disabled={!!inst.managed}
                      placeholder="http://gitea:3000"
                      onChange={(e) => setField(`cfg.pmos.${idx}.api_base`,
                        e.target.value.trim() || null)} />
                  </Field>
                )}
                {inst.managed ? (
                  <Field label="API key">
                    <p className="text-xs text-neutral-500 dark:text-neutral-400">
                      App-minted and self-healing — a revoked or lost key is
                      re-minted at the next boot or config save. Nothing to
                      paste here.
                    </p>
                  </Field>
                ) : (
                  <SecretField label="API key"
                    help="This instance's PMO API key. Stored as plaintext mode 0600 on the app volume — never echoed back, never in .env."
                    refKey={`pmo:${inst.name}:api_key`} paste
                    locked={!pmoNameLocked(inst.name, idx)} />
                )}
              </div>
              {/* repo pickers: their OWN grid with fixed slots, so the
                  controls sit in the same place on every card no matter
                  how many system-specific fields the grid above holds
                  (founder, 2026-08-14: buttons must not wander) */}
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div>
                  <RepoChips label="Repositories"
                    help="The repositories this board's tickets may change. Only repos with a stored Access token qualify (writing needs push access). The first selected is the default; a ticket picks another listed repo with a `devcake-repo:<name>` line in its description. With none selected, DevCake creates a private repo per ticket on the bundled forge."
                    all={cfg.repos} selected={inst.repos || []}
                    excluded={[...(inst.reference_repos || []), ...(inst.memory_repos || [])]}
                    excludedNote="reference or memory notebook"
                    unavailable={cfg.repos.map((r) => r.name).filter((n) => !repoHasToken[n])}
                    unavailableNote="no Access token stored — usable only as a reference repo"
                    firstBadge=" · default"
                    onChange={(next) => setField(`cfg.pmos.${idx}.repos`, next)} />
                  {dr.errors[`cfg.pmos.${idx}.repos`] && (
                    <p className="text-xs text-red-600 dark:text-red-400">
                      ✗ {dr.errors[`cfg.pmos.${idx}.repos`]}
                    </p>
                  )}
                </div>
                <div>
                  <RepoChips label="Reference repos"
                    help="Read-only background material — docs, style guides, related code — cloned next to the work repository in every run. Workers can read it, never change it."
                    all={cfg.repos} selected={inst.reference_repos || []}
                    excluded={[...(inst.repos || []), ...(inst.memory_repos || [])]}
                    excludedNote="work repo or memory notebook"
                    onChange={(next) => setField(`cfg.pmos.${idx}.reference_repos`, next)} />
                  {dr.errors[`cfg.pmos.${idx}.reference_repos`] && (
                    <p className="text-xs text-red-600 dark:text-red-400">
                      ✗ {dr.errors[`cfg.pmos.${idx}.reference_repos`]}
                    </p>
                  )}
                </div>
                <div>
                  <RepoChips label="Memory (board-bound)"
                    help="Team-memory notebooks this board's workers consult while working — mounted read-only in every run. A notebook is an ordinary repository card that holds curated notes; it is never a work target here (a separate Curator board maintains it — see Configuration → Scheduled Tasks)."
                    all={cfg.repos} selected={inst.memory_repos || []}
                    excluded={[...(inst.repos || []), ...(inst.reference_repos || [])]}
                    excludedNote="work or reference repo"
                    onChange={(next) => setField(`cfg.pmos.${idx}.memory_repos`, next)} />
                  {dr.errors[`cfg.pmos.${idx}.memory_repos`] && (
                    <p className="text-xs text-red-600 dark:text-red-400">
                      ✗ {dr.errors[`cfg.pmos.${idx}.memory_repos`]}
                    </p>
                  )}
                </div>
              </div>
              <SettingRow label="Discovery routing"
                desc={inst.discovery_routing === false
                  ? "OFF — discoveries are still recorded on their source ticket; nothing is shared across related tickets until re-enabled."
                  : "ON — a steward run shares each worker discovery with the related tickets that should see it (applies on Save)."}
                help="Workers may report discoveries — surprising findings with evidence. Each is always recorded on the ticket that produced it. With routing on, a steward run decides which RELATED tickets should also see the finding and delivers it there as an advisory comment (leads, not truths). Turning this off leaves pending discoveries visible on the board under the DEVCAKE-DISCOVERY label for a later toggle-on.">
                <Toggle on={inst.discovery_routing !== false}
                  label={`Discovery routing for ${inst.name}`}
                  onClick={() => setField(`cfg.pmos.${idx}.discovery_routing`,
                    inst.discovery_routing === false)} />
              </SettingRow>
              <div className="flex flex-wrap items-center gap-3">
                <Button kind="ghost" onClick={() => testPmo(inst.name)}>Test connection</Button>
                <ImmediateBadge text="tests saved values" />
                {tr && (
                  <span className={`text-sm ${tr.ok ? "text-green-700 dark:text-green-400" : "text-red-600"}`}>
                    {tr.ok
                      ? `✓ team ${tr.team}: ${tr.labels}/${tr.labels_expected ?? 10} labels, ${tr.missions_visible} items visible`
                      : `✗ ${tr.error || tr.detail || "connection test failed"}`}
                  </span>
                )}
              </div>
            </div>
          );
        })}
        <Button kind="ghost" onClick={() => {
          const name = nextFreeName("linear", cfg.pmos, dr.server.cfg.pmos);
          newPmoNames.track(name);
          setExpandedPmos((prev) => new Set(prev).add(cfg.pmos.length));
          const defaultSystem = (registry.pmo_systems || [])[0]?.id || "linear";
          setField("cfg.pmos", [...cfg.pmos, newPmoCard(name, defaultSystem)]);
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
      {clearSecrets && (
        <ClearSecretsDialog
          context="pmo"
          onClose={() => setClearSecrets(false)}
          onCleared={async (result) => {
            // Swallow reload errors so the ConfirmDialog does not re-label a
            // successful delete as a failed clear (Fable PR #54 review).
            try {
              setClearReloadErr("");
              setTestResult({});
              await reload();
              setSecretsEpoch((e) => e + 1);
              if (result?.intake_paused) {
                onHealthChange?.((h) => ({ ...h, intake_paused: true }));
              }
            } catch (e) {
              setClearReloadErr(
                `reload after clear secrets failed: ${String(e.message || e)}`);
            }
          }}
        />
      )}
    </>
  );
}
