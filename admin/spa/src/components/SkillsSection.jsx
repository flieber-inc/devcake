import React, { useEffect, useRef, useState } from "react";
import { Eye, Plus, Trash2 } from "lucide-react";
import { get, send } from "../api.js";
import { Section } from "./Card.jsx";
import { Field, Input, Textarea } from "./Field.jsx";
import Button from "./Button.jsx";
import { ConfirmDialog, Modal } from "./Modal.jsx";
import MoreMenu from "./MoreMenu.jsx";
import ImmediateBadge from "./ImmediateBadge.jsx";
import MarkdownBody, {
  MarkdownModeToggle,
  MarkdownSourcePre,
  MarkdownViewShell,
} from "./MarkdownBody.jsx";
import { SecretField, Select } from "./Field.jsx";
import { newSkillSourceCard } from "../lib/cards.js";
import { getRegistry, loadRegistry } from "../lib/registry.js";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";
import { useNewNames } from "../lib/instanceNames.js";
import { fileToB64 } from "../lib/files.js";
import { isMarkdownPath, stripYamlFrontmatter } from "../lib/markdown.js";
import { connRef } from "../lib/connectionFields.js";

// ── skill authoring (docs/11 Skills section) ─────────────────────────────────

// "Add skill" dialog: Write (name + trigger + markdown; the app generates
// the frontmatter — the operator never sees YAML) or Import (upload a
// SKILL.md + optional supporting files). 409 flips into an explicit
// overwrite confirmation instead of silently replacing.
function AddSkillDialog({ onClose, onSaved }) {
  const [mode, setMode] = useState("write");
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [body, setBody] = useState("");
  const [files, setFiles] = useState([]);      // [{path, content_b64}]
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [askOverwrite, setAskOverwrite] = useState(false);

  const canSubmit = mode === "write"
    ? name.trim() && desc.trim() && body.trim()
    : files.length > 0;

  // any content edit invalidates a pending 409/overwrite decision — the
  // collision was for the OLD name/payload, so re-check from scratch
  const edited = (setter) => (v) => { setter(v); setAskOverwrite(false); setErr(""); };

  const submit = async (overwrite) => {
    setBusy(true); setErr("");
    try {
      if (mode === "write")
        await send("POST", "/skills",
          { name: name.trim(), description: desc.trim(), body, overwrite });
      else
        await send("POST", "/skills/import", { files, overwrite });
      onSaved(); onClose();
    } catch (e) {
      setAskOverwrite(e.status === 409);
      setErr(String(e.message || e).replace(/^\d+ /, ""));
    } finally { setBusy(false); }
  };

  return (
    <Modal className="max-w-2xl" onClose={busy ? undefined : onClose}>
      <div className="mb-4 flex items-center justify-between">
        <h4 className="text-base font-semibold tracking-tight">Add skill</h4>
        <div className="flex gap-1 rounded-md bg-stone-100 p-0.5 text-xs dark:bg-neutral-800">
          {[["write", "Write"], ["import", "Import files"]].map(([m, l]) => (
            <button key={m} type="button"
              onClick={() => { setMode(m); setErr(""); setAskOverwrite(false); }}
              className={`rounded px-2.5 py-1 font-medium transition ${
                mode === m ? "bg-white shadow-sm dark:bg-neutral-700"
                           : "text-neutral-500 hover:text-neutral-700 dark:hover:text-neutral-300"}`}>
              {l}
            </button>
          ))}
        </div>
      </div>
      {mode === "write" ? (
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="Name" hint="lowercase, - or _ (e.g. release-notes)">
              <Input value={name} onChange={(e) => edited(setName)(e.target.value)}
                placeholder="my-skill" />
            </Field>
            <Field label="When should the agent use it?"
              hint="this description is the trigger — be specific">
              <Input value={desc} onChange={(e) => edited(setDesc)(e.target.value)}
                placeholder="Writes release notes: use when a release is being prepared." />
            </Field>
          </div>
          <Field label="Instructions (markdown)">
            <Textarea rows={10} value={body}
              onChange={(e) => edited(setBody)(e.target.value)}
              placeholder={"# Release notes\n\nStep-by-step guidance the agent should follow…"} />
          </Field>
        </div>
      ) : (
        <div className="space-y-3">
          <Field label="Skill folder"
            hint="pick the skill's folder (containing SKILL.md) — nested files keep their layout; the name comes from the frontmatter">
            {/* webkitdirectory: a plain multi-file picker only exposes
                basenames, which silently flattens refs/x.md → x.md and
                collides same-named files. A folder pick carries
                webkitRelativePath, so the layout under the skill dir survives. */}
            <input type="file" webkitdirectory="" directory=""
              className="block w-full text-sm text-neutral-500 file:mr-3 file:rounded-md file:border-0 file:bg-stone-100 file:px-3 file:py-1.5 file:text-sm file:font-medium dark:file:bg-neutral-800"
              onChange={async (e) => {
                setErr(""); setAskOverwrite(false);
                const fs = [];
                for (const f of e.target.files) {
                  // "<folder>/<rel>" → drop the chosen folder's own name
                  const rel = (f.webkitRelativePath || f.name)
                    .split("/").slice(1).join("/") || f.name;
                  // skip OS/VCS cruft a folder pick sweeps in (.DS_Store, .git/…)
                  if (rel.split("/").some((seg) => seg.startsWith("."))) continue;
                  fs.push({ path: rel, content_b64: await fileToB64(f) });
                }
                setFiles(fs);
              }} />
          </Field>
          {files.length > 0 && (
            <p className="text-xs text-neutral-500 dark:text-neutral-400">
              {files.length} file(s): {files.map((f) => f.path).join(", ")}
            </p>
          )}
        </div>
      )}
      {err && (
        <p className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300">
          {err}
        </p>
      )}
      <div className="mt-5 flex justify-end gap-2">
        <Button kind="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
        {askOverwrite ? (
          <Button kind="danger" disabled={busy || !canSubmit} onClick={() => submit(true)}>
            {busy ? "Working…" : "Overwrite existing skill"}
          </Button>
        ) : (
          <Button disabled={busy || !canSubmit} onClick={() => submit(false)}>
            {busy ? "Working…" : "Save to store"}
          </Button>
        )}
      </div>
    </Modal>
  );
}

// Read-only skill viewer: store-first content (or bundled) for operators who
// need to inspect a skill without opening Gitea. Multi-file skills get tabs.
// .md files: Rendered Markdown (frontmatter stripped) + Source (stored bytes).
function ViewSkillDialog({ name, onClose }) {
  const [detail, setDetail] = useState(null);
  const [err, setErr] = useState("");
  const [file, setFile] = useState("SKILL.md");
  const [mode, setMode] = useState("rendered"); // rendered | source

  useEffect(() => {
    let cancelled = false;
    setDetail(null); setErr(""); setFile("SKILL.md"); setMode("rendered");
    get(`/skills/${encodeURIComponent(name)}`)
      .then((r) => {
        if (cancelled) return;
        setDetail(r);
        const paths = (r.files || []).map((f) => f.path);
        setFile(paths.includes("SKILL.md") ? "SKILL.md" : (paths[0] || ""));
      })
      .catch((e) => {
        if (!cancelled) setErr(String(e.message || e).replace(/^\d+ /, ""));
      });
    return () => { cancelled = true; };
  }, [name]);

  const content = detail?.files?.find((f) => f.path === file)?.content ?? "";
  const md = isMarkdownPath(file);
  // non-md tabs always show raw source; toggle only applies to markdown files
  const showRendered = md && mode === "rendered";

  return (
    <Modal className="max-w-3xl" onClose={onClose}>
      <div className="mb-3 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h4 className="font-mono text-base font-semibold tracking-tight">
            {name}
          </h4>
          {detail && (
            <p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">
              {detail.description || "(no description)"}
            </p>
          )}
        </div>
        <div className="flex shrink-0 flex-col items-end gap-2">
          {detail && (
            <span className={"rounded px-1.5 py-0.5 text-xs "
              + (detail.source === "store"
                ? "bg-stone-100 text-stone-700 dark:bg-neutral-800 dark:text-neutral-300"
                : "bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300")}>
              {detail.source === "store" ? "store" : "bundled"}
              {detail.builtin ? " · built-in" : ""}
            </span>
          )}
          {detail && md && (
            <MarkdownModeToggle mode={mode} onChange={setMode} />
          )}
        </div>
      </div>
      {err && (
        <p className="mb-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300">
          {err}
        </p>
      )}
      {!err && !detail && (
        <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading…</p>
      )}
      {detail && (
        <>
          {(detail.files || []).length > 1 && (
            <div className="mb-2 flex flex-wrap gap-1">
              {detail.files.map((f) => (
                <button key={f.path} type="button"
                  onClick={() => setFile(f.path)}
                  className={`rounded-md border px-2 py-0.5 font-mono text-xs transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 ${
                    file === f.path
                      ? "border-accent-400 bg-accent-50 text-accent-800 dark:border-accent-700 dark:bg-accent-950/70 dark:text-accent-200"
                      : "border-neutral-300 text-neutral-500 hover:bg-stone-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
                  }`}>
                  {f.path}
                </button>
              ))}
            </div>
          )}
          <MarkdownViewShell>
            {showRendered ? (
              <MarkdownBody>{stripYamlFrontmatter(content)}</MarkdownBody>
            ) : (
              <MarkdownSourcePre>{content}</MarkdownSourcePre>
            )}
          </MarkdownViewShell>
        </>
      )}
      <div className="mt-5 flex justify-end">
        <Button kind="ghost" onClick={onClose}>Close</Button>
      </div>
    </Modal>
  );
}

// Dedicated skills connections (2026-08-14 ruling): a skills repository
// is its own connection — managed HERE, never on the Repositories page.
function SkillSourcesCard({ setPageErr, onCatalogReload }) {
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
      await send("POST", "/skills/sources/refresh");
      await onCatalogReload();
      setRefreshMsg("✓ skill sources updated");
      clearTimeout(refreshTimer.current);
      refreshTimer.current = setTimeout(() => setRefreshMsg(""), 4000);
    } catch (e) {
      setPageErr(`skill source update failed: ${String(e.message || e)}`);
    }
  };

  return (
    <Section id="skills-sources" title="Skill sources"
      description="Repositories that hold skills — connected here, separate from your code repositories."
      help="A skill source is a git repository whose folders each hold one skill (a SKILL.md plus supporting files). Connect one here and every skill inside it appears in the catalog above as <source>/<skill>, ready to attach to worker profiles. Sources are read-only: DevCake fetches them through its mirror before every run and never writes to them. They are deliberately NOT repository cards — nothing here can become a work target."
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
          No skill sources yet — the catalog above lists the bundled
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
              help="Short identity for this source (lowercase letters/digits, up to 12). Skills from it are selected as '<name>/<skill>' on worker profiles. Locked once saved — stored tokens key on it; remove and re-add to rename.">
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

export default function SkillsSection({ setPageErr }) {
  const [confirm, setConfirm] = useState(null); // delete confirms
  // skill store catalog (v1): store-listed when Gitea is up, bundled
  // fallback otherwise — `store` says which (and where to edit)
  const [skillsCatalog, setSkillsCatalog] = useState({ skills: [], store: null });
  const [skillsErr, setSkillsErr] = useState(false);
  const [skillsMsg, setSkillsMsg] = useState("");
  const skillsMsgTimer = useRef(null);
  useEffect(() => () => clearTimeout(skillsMsgTimer.current), []);
  const loadSkills = () =>
    get("/skills")
      .then((r) => { setSkillsCatalog(r); setSkillsErr(false); })
      .catch(() => setSkillsErr(true));
  useEffect(() => { loadSkills(); }, []);
  const restoreBuiltins = async () => {
    try {
      await send("POST", "/skills/sync");
      await loadSkills();
      setSkillsMsg("✓ built-in skills restored");
      clearTimeout(skillsMsgTimer.current);
      skillsMsgTimer.current = setTimeout(() => setSkillsMsg(""), 4000);
    } catch (e) {
      setPageErr(`restoring built-ins failed: ${String(e.message || e)}`);
    }
  };
  const [addSkill, setAddSkill] = useState(false);
  const [viewSkill, setViewSkill] = useState(null);

  return (
    <>
      <Section id="skills" title="Skills"
        description="Reusable expertise installed into the agent session."
        help="Domain skill modules Devs may consult. Attach per Dev Type as Available (optional) or Required (soft-force prompt). Skills must be domain-specific and additive — never mission-step scripts."
        actions={skillsCatalog.store?.enabled && (
          <>
            <Button icon={Plus} onClick={() => setAddSkill(true)}>
              Add skill
            </Button>
            {/* no html_url → the menu would hold a single item; DESIGN §3
                says a lone action stays a visible ghost button instead */}
            {skillsCatalog.store?.html_url ? (
              <MoreMenu label="More skill-store actions" items={[
                {
                  label: "Open the store in Gitea",
                  desc: "Skills live in a Git repo — edit them there directly.",
                  external: true,
                  onClick: () => window.open(skillsCatalog.store.html_url, "_blank", "noopener"),
                },
                {
                  label: "Restore built-in skills",
                  desc: "Re-adds any missing bundled skills. Never overwrites your edits.",
                  onClick: restoreBuiltins,
                },
              ]} />
            ) : (
              <Button kind="ghost" onClick={restoreBuiltins}>
                Restore built-in skills
              </Button>
            )}
          </>
        )}>
        {skillsCatalog.store && !skillsCatalog.store.enabled && (
          <p className="mb-3 text-sm text-neutral-500 dark:text-neutral-400">
            Served from the bundled copies — set GITEA_ADMIN_PASSWORD (bundled
            Gitea) to get the editable skill-store repo.
          </p>
        )}
        {skillsMsg && (
          <p className="mb-3 text-sm text-green-700 dark:text-green-400">{skillsMsg}</p>
        )}
        {skillsCatalog.store?.enabled && !skillsCatalog.store.ok && (
          <p className="mb-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-200">
            Skill store unreachable ({skillsCatalog.store.detail}) — serving the
            bundled copies. Runs keep working; store edits are unavailable.
          </p>
        )}
        {skillsErr ? (
          <p className="text-sm text-red-700 dark:text-red-300">
            Couldn&apos;t load the skill catalog.{" "}
            <button type="button" onClick={loadSkills}
              className="font-medium underline underline-offset-2">
              Retry
            </button>
          </p>
        ) : skillsCatalog.skills.length === 0 ? (
          <p className="text-sm text-neutral-500 dark:text-neutral-400">No skills found.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[36rem] text-left text-sm">
              <thead className="text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
                <tr>
                  <th className="py-1.5 pr-4 font-semibold">Skill</th>
                  <th className="pr-4 font-semibold">Description</th>
                  <th className="pr-2 font-semibold">Source</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {skillsCatalog.skills.map((s) => (
                  <tr key={s.name} className="border-t border-neutral-100 align-top dark:border-neutral-800">
                    <td className="whitespace-nowrap py-2.5 pr-4 font-mono text-xs font-semibold">
                      {s.name}
                    </td>
                    <td className="py-2.5 pr-4 text-neutral-500 dark:text-neutral-400">
                      {s.description || "(no description)"}
                    </td>
                    <td className="py-2.5 pr-2">
                      <span className={"inline-block whitespace-nowrap rounded px-1.5 py-0.5 text-xs "
                        + (s.source === "store"
                          ? "bg-stone-100 text-stone-700 dark:bg-neutral-800 dark:text-neutral-300"
                          : s.source === "external"
                            ? "bg-accent-50 text-accent-800 dark:bg-accent-950/70 dark:text-accent-200"
                            : "bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300")}
                        title={s.source === "external"
                          ? `Served read-only from skill source '${s.origin}' (its mirror syncs before every dispatch)`
                          : undefined}>
                        {s.source === "store" ? "store"
                          : s.source === "external" ? `source: ${s.origin}` : "bundled"}
                      </span>
                    </td>
                    <td className="py-2.5 text-right">
                      {/* Fixed action pair on every row so the column never
                          shifts: View is always live; Delete is always the
                          same control when the store is editable — disabled
                          for built-ins (they re-seed; deselect on Dev Types).
                          Retired names still in the store are not builtins
                          and stay deletable so operators can clean them. */}
                      <div className="flex items-center justify-end gap-1">
                        <button type="button"
                          title={`View skill ${s.name}`}
                          aria-label={`View skill ${s.name}`}
                          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium text-neutral-600 hover:bg-stone-100 hover:text-neutral-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:text-neutral-400 dark:hover:bg-neutral-800 dark:hover:text-neutral-100"
                          onClick={() => setViewSkill(s.name)}>
                          <Eye className="h-3.5 w-3.5" aria-hidden />
                          View
                        </button>
                        {skillsCatalog.store?.enabled && (
                          <button type="button"
                            disabled={s.builtin || s.source === "external"}
                            title={s.builtin
                              ? `${s.name} is a built-in skill — it re-seeds at boot. Deselect it on Dev Types instead of deleting.`
                              : s.source === "external"
                                ? `${s.name} is read-only — served from skill source '${s.origin}'. Deselect it on Dev Types or edit it in that repository.`
                                : `Delete skill ${s.name}`}
                            aria-label={s.builtin || s.source === "external"
                              ? `${s.name} cannot be deleted (${s.builtin ? "built-in" : "external"})`
                              : `Delete skill ${s.name}`}
                            className={`rounded p-0.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 ${
                              s.builtin || s.source === "external"
                                ? "cursor-not-allowed text-neutral-300 dark:text-neutral-700"
                                : "text-neutral-500 hover:text-red-600 dark:text-neutral-400 dark:hover:text-red-400"
                            }`}
                            onClick={() => {
                              if (s.builtin || s.source === "external") return;
                              setConfirm({
                                title: `Delete skill ${s.name}?`,
                                body: "Removed from the skill store. Dev Types that "
                                  + "selected it keep the name (⚠) but the skill is "
                                  + "skipped at dispatch until re-added.",
                                confirmLabel: "Delete",
                                action: async () => {
                                  try {
                                    await send("DELETE", `/skills/${encodeURIComponent(s.name)}`);
                                    await loadSkills();
                                  } catch (e) {
                                    setPageErr(`skill delete failed: ${String(e.message || e)}`);
                                  }
                                  setConfirm(null);
                                },
                              });
                            }}>
                            <Trash2 className="h-4 w-4" />
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <SkillSourcesCard setPageErr={setPageErr} onCatalogReload={loadSkills} />

      <ConfirmDialog open={!!confirm} {...(confirm || {})}
        onConfirm={() => confirm.action()}
        onCancel={() => setConfirm(null)} />
      {addSkill && <AddSkillDialog
        onClose={() => setAddSkill(false)} onSaved={loadSkills} />}
      {viewSkill && (
        <ViewSkillDialog name={viewSkill}
          onClose={() => setViewSkill(null)} />
      )}
    </>
  );
}
