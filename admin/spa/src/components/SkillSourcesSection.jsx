import React, { useEffect, useRef, useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { send } from "../api.js";
import { Section } from "./Card.jsx";
import { Field, Input, SecretField, Select } from "./Field.jsx";
import Button from "./Button.jsx";
import ImmediateBadge from "./ImmediateBadge.jsx";
import { newSkillSourceCard } from "../lib/cards.js";
import { getRegistry, loadRegistry } from "../lib/registry.js";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";
import { useNewNames } from "../lib/instanceNames.js";
import { connRef } from "../lib/connectionFields.js";

// Dedicated skills connections (2026-08-14 ruling): a skills repository
// is its own connection — managed under Connections → Skill sources
// (CAKE-159), never on the Repositories page or the Fleet skills catalog.
export default function SkillSourcesSection({
  setPageErr, onCatalogReload = async () => {},
}) {
  const { dr, skillSourceNewNamesState } = useSharedDraft();
  const [registry, setRegistry] = useState(getRegistry());
  const [testResult, setTestResult] = useState({});
  const [refreshMsg, setRefreshMsg] = useState("");
  const refreshTimer = useRef(null);
  useEffect(() => { loadRegistry().then(setRegistry); }, []);
  useEffect(() => () => clearTimeout(refreshTimer.current), []);
  const cfg = dr.draft.cfg;
  const setField = dr.setField;
  const sources = cfg.skill_sources || [];
  // stored tokens key on the source name — lock the name once saved, with
  // the same session-new / last-index rule as Repos and PMO cards
  const newNames = useNewNames(dr.server?.cfg.skill_sources, sources,
                               skillSourceNewNamesState);
  const savedNames = new Set(
    (dr.server.cfg.skill_sources || []).map((x) => x.name));
  const nameLocked = (name, idx) =>
    savedNames.has(name) &&
    !(newNames.has(name) && idx === sources.map((s) => s.name).lastIndexOf(name));

  const testSkill = async (name) => {
    const key = `skill:${name}`;
    try {
      const result = await send(
        "POST", `/connections/skill/${encodeURIComponent(name)}/test`);
      setTestResult((prev) => ({ ...prev, [key]: result }));
    } catch (e) {
      setTestResult((prev) => ({
        ...prev,
        [key]: { ok: false, error: String(e.message || e) },
      }));
    }
  };

  const updateNow = async () => {
    try {
      const res = await send("POST", "/skills/sources/refresh");
      await onCatalogReload();
      const failed = Object.entries(res?.failures || {});
      if (failed.length) {
        // honest badge: a failed fetch must never show a green ✓
        setRefreshMsg("");
        setPageErr("skill source update failed for " +
          failed.map(([n, r]) => `${n} (${r})`).join("; "));
        return;
      }
      setRefreshMsg("✓ skill sources updated");
      clearTimeout(refreshTimer.current);
      refreshTimer.current = setTimeout(() => setRefreshMsg(""), 4000);
    } catch (e) {
      setPageErr(`skill source update failed: ${String(e.message || e)}`);
    }
  };

  return (
    <Section id="skills-sources" title="Skill sources"
      description="Repositories that hold skills — connected here under Connections, separate from your code repositories. The installed catalog lives under Fleet → Skills."
      help="A skill source is a git repository whose folders each hold one skill (a SKILL.md plus supporting files). Connect one here and every skill inside it appears in Fleet → Skills as <source>/<skill>, ready to attach to worker profiles. Sources are read-only: DevCake fetches them through its mirror before every run and never writes to them. They are deliberately NOT repository cards — nothing here can become a work target."
      actions={
        <>
          <Button onClick={updateNow}>Update now</Button>
          <ImmediateBadge text="refreshes mirrors now" />
        </>
      }>
      {refreshMsg && (
        <p className="mb-3 text-sm text-green-700 dark:text-green-400">{refreshMsg}</p>
      )}
      {sources.length === 0 && (
        <p className="text-sm text-neutral-500 dark:text-neutral-400">
          No skill sources yet — Fleet → Skills lists the bundled
          skill store only.
        </p>
      )}
      {sources.map((src, idx) => {
        const locked = nameLocked(src.name, idx);
        const tr = testResult[`skill:${src.name}`];
        return (
        <div key={idx}
          className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-mono text-sm font-semibold">
              {src.name || "(unnamed)"}
            </span>
            <Button kind="danger-ghost" icon={Trash2} size="sm"
              aria-label={`Remove skill source ${src.name}`}
              onClick={() => {
                newNames.untrack(src.name);
                setField("cfg.skill_sources",
                  sources.filter((_, i) => i !== idx));
              }}>
              Remove
            </Button>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field label="Name"
              help="Short identity for this source (lowercase letters/digits/underscores, ≤39, no hyphens). Skills from it are selected as '<name>/<skill>' on worker profiles. Locked once saved — stored tokens key on it; remove and re-add to rename.">
              <Input value={src.name}
                aria-label={`Skill source ${idx + 1} name`}
                disabled={locked}
                onChange={(e) => {
                  newNames.rename(src.name, e.target.value);
                  setField(`cfg.skill_sources.${idx}.name`, e.target.value);
                }} />
              {dr.errors[`cfg.skill_sources.${idx}.name`] && (
                <span className="mt-1 block text-xs text-red-600 dark:text-red-400">
                  ✗ {dr.errors[`cfg.skill_sources.${idx}.name`]}
                </span>
              )}
            </Field>
            <Field label="Forge"
              help="Which service hosts the repository — used only to shape the authenticated fetch.">
              <Select value={src.forge || "github"}
                aria-label={`Skill source ${idx + 1} forge`}
                onChange={(e) => setField(`cfg.skill_sources.${idx}.forge`,
                  e.target.value)}>
                {(registry.forges || []).map((f) => (
                  <option key={f.id} value={f.id}>{f.id}</option>
                ))}
              </Select>
            </Field>
            <Field label="Repository URL"
              help="HTTPS URL of the skills repository, e.g. https://github.com/you/skills.git.">
              <Input value={src.url || ""}
                aria-label={`Skill source ${idx + 1} URL`}
                onChange={(e) => setField(`cfg.skill_sources.${idx}.url`,
                  e.target.value)} />
            </Field>
            <Field label="Branch" hint="Empty = the repository's default">
              <Input value={src.default_branch || ""}
                aria-label={`Skill source ${idx + 1} branch`}
                onChange={(e) => setField(`cfg.skill_sources.${idx}.default_branch`,
                  e.target.value)} />
            </Field>
            <Field label="Skills folder" hint="Empty = repository root"
              help="Path inside the repository where the skill folders live, e.g. 'skills'.">
              <Input value={src.subdir || ""} placeholder="e.g. skills"
                aria-label={`Skill source ${idx + 1} folder`}
                onChange={(e) => setField(`cfg.skill_sources.${idx}.subdir`,
                  e.target.value)} />
            </Field>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <SecretField label="Read token"
              help="Token with read access to the repository (private sources). Stored as plaintext mode 0600 on the app volume — never echoed back."
              refKey={connRef("skill", src.name, "token_ro")} paste
              locked={!locked} />
            <SecretField label="Token (fallback)"
              help="Used only when no read token is stored. A read-scoped token is all a skills source ever needs."
              refKey={connRef("skill", src.name, "token")} paste
              locked={!locked} />
          </div>
          {!locked && (
            <p className="text-xs text-neutral-500 dark:text-neutral-400">
              Save first — tokens can be pasted once the source exists.
            </p>
          )}
          {locked && (
            <div className="flex flex-wrap items-center gap-3">
              <Button kind="ghost" onClick={() => testSkill(src.name)}>
                Test connection
              </Button>
              <ImmediateBadge text="tests saved values" />
              {tr && (
                <span className={`text-sm ${tr.ok
                  ? "text-green-700 dark:text-green-400"
                  : "text-red-600 dark:text-red-400"}`}>
                  {tr.ok
                    ? `✓ reachable (${tr.forge}): ${(tr.remote_head || "").slice(0, 12) || "ok"}`
                    : `✗ ${tr.error || tr.detail || "connection test failed"}`}
                </span>
              )}
            </div>
          )}
        </div>
        );
      })}
      <button type="button"
        onClick={() => {
          newNames.track("");
          setField("cfg.skill_sources",
            [...sources, newSkillSourceCard("")]);
        }}
        className="flex w-full items-center justify-center gap-2 rounded-card border-2 border-dashed border-neutral-300 py-2.5 text-sm font-medium text-neutral-600 transition hover:border-accent-400 hover:bg-accent-50/40 hover:text-accent-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:border-neutral-700 dark:text-neutral-300 dark:hover:border-accent-700 dark:hover:bg-accent-950/30 dark:hover:text-accent-300">
        <Plus size={15} aria-hidden="true" />
        New skill source
      </button>
    </Section>
  );
}
