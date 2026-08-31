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
import { fileToB64, textToB64 } from "../lib/files.js";
import { isMarkdownPath, stripYamlFrontmatter } from "../lib/markdown.js";

// ── skill authoring (docs/11 Skills section) ─────────────────────────────────

// CAKE-165 deep-link: #/fleet/skills?add=1 auto-opens Add skill (then clears).
function hashWantsAddSkill() {
  const hash = window.location.hash || "";
  const q = hash.includes("?") ? hash.slice(hash.indexOf("?") + 1) : "";
  return new URLSearchParams(q.split("#")[0]).get("add") === "1";
}

function clearAddSkillFlag() {
  const hash = window.location.hash || "";
  if (!hash.includes("?")) return;
  const next = "#/fleet/skills";
  if (hash.split("?")[0] !== next && !hash.startsWith("#/fleet/skills")) return;
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${next}`);
}

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
      <p className="mt-4 text-sm text-neutral-500 dark:text-neutral-400">
        Skills live in an external repository?{" "}
        <a href="#/skill-sources"
          className="font-medium text-accent-700 underline underline-offset-2 dark:text-accent-300">
          Connect it as a skill source
        </a>
      </p>
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

// Skill viewer: store skills get editable Source + live Rendered; external
// skills stay read-only with an honest repository notice (CAKE-166).
function ViewSkillDialog({ name, onClose, skillSources = [], repos = [],
                          onSaved }) {
  const [detail, setDetail] = useState(null);
  const [err, setErr] = useState("");
  const [file, setFile] = useState("SKILL.md");
  const [mode, setMode] = useState("rendered"); // rendered | source
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);

  const load = () => {
    setDetail(null); setErr(""); setFile("SKILL.md"); setMode("rendered");
    setDraft(""); setDirty(false);
    return get(`/skills/${encodeURIComponent(name)}`)
      .then((r) => {
        setDetail(r);
        const paths = (r.files || []).map((f) => f.path);
        const initial = paths.includes("SKILL.md") ? "SKILL.md" : (paths[0] || "");
        setFile(initial);
        setDraft(r.files?.find((f) => f.path === initial)?.content ?? "");
      })
      .catch((e) => {
        setErr(String(e.message || e).replace(/^\d+ /, ""));
      });
  };

  useEffect(() => {
    let cancelled = false;
    setDetail(null); setErr(""); setFile("SKILL.md"); setMode("rendered");
    setDraft(""); setDirty(false);
    get(`/skills/${encodeURIComponent(name)}`)
      .then((r) => {
        if (cancelled) return;
        setDetail(r);
        const paths = (r.files || []).map((f) => f.path);
        const initial = paths.includes("SKILL.md") ? "SKILL.md" : (paths[0] || "");
        setFile(initial);
        setDraft(r.files?.find((f) => f.path === initial)?.content ?? "");
      })
      .catch((e) => {
        if (!cancelled) setErr(String(e.message || e).replace(/^\d+ /, ""));
      });
    return () => { cancelled = true; };
  }, [name]);

  const selectFile = (path) => {
    setFile(path);
    setDraft(detail?.files?.find((f) => f.path === path)?.content ?? "");
    setDirty(false);
    setErr("");
  };

  const editable = detail?.source === "store";
  const external = detail?.source === "external";
  const originSrc = external
    ? skillSources.find((s) => s.name === detail.origin)
    : null;
  const originUrl = external
    ? (originSrc?.url
      || (originSrc?.backed_by
        ? (repos.find((r) => r.name === originSrc.backed_by)?.url || "")
        : ""))
    : "";
  const md = isMarkdownPath(file);
  const showRendered = md && mode === "rendered";
  const sourceLabel = detail?.source === "store" ? "store"
    : detail?.source === "external" ? `source: ${detail.origin}`
      : "bundled";

  const save = async () => {
    if (!detail || !editable) return;
    setBusy(true); setErr("");
    try {
      const files = (detail.files || []).map((f) => ({
        path: f.path,
        content_b64: textToB64(f.path === file ? draft : (f.content ?? "")),
      }));
      await send("POST", "/skills/import", { files, overwrite: true });
      setDirty(false);
      onSaved?.();
      await load();
    } catch (e) {
      setErr(String(e.message || e).replace(/^\d+ /, ""));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal className="max-w-3xl" onClose={busy ? undefined : onClose}>
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
                : detail.source === "external"
                  ? "bg-accent-50 text-accent-800 dark:bg-accent-950/70 dark:text-accent-200"
                  : "bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300")}>
              {sourceLabel}
              {detail.builtin ? " · built-in" : ""}
            </span>
          )}
          {detail && md && (
            <MarkdownModeToggle mode={mode} onChange={setMode} />
          )}
        </div>
      </div>
      {external && (
        <p className="mb-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-200"
          data-testid="skill-external-readonly">
          This skill is read-only here — changes must be made in the external
          repository
          {detail?.origin ? ` (${detail.origin})` : ""}.
          {originUrl ? (
            <>
              {" "}
              <a href={originUrl} target="_blank" rel="noopener noreferrer"
                className="font-medium underline underline-offset-2">
                Open repository ↗
              </a>
            </>
          ) : (
            <> No repository URL is configured on that skill source.</>
          )}
        </p>
      )}
      {editable && (
        <p className="mb-3 text-xs text-neutral-500 dark:text-neutral-400">
          Source edits save immediately to the skill store (overwrite).
        </p>
      )}
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
                  onClick={() => selectFile(f.path)}
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
              <MarkdownBody>{stripYamlFrontmatter(draft)}</MarkdownBody>
            ) : editable ? (
              <Textarea rows={18}
                className="min-h-[40vh] border-0 bg-transparent font-mono text-xs shadow-none focus:ring-0"
                value={draft}
                aria-label="Skill source"
                onChange={(e) => { setDraft(e.target.value); setDirty(true); }} />
            ) : (
              <MarkdownSourcePre>{draft}</MarkdownSourcePre>
            )}
          </MarkdownViewShell>
        </>
      )}
      <div className="mt-5 flex justify-end gap-2">
        <Button kind="ghost" disabled={busy} onClick={onClose}>Close</Button>
        {editable && (
          <Button disabled={busy || !dirty} onClick={save}>
            {busy ? "Saving…" : "Save skill"}
          </Button>
        )}
      </div>
    </Modal>
  );
}


export default function SkillsSection({ setPageErr, skillSources = [],
                                        repos = [] }) {
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
  const fetchExternal = async () => {
    try {
      const res = await send("POST", "/skills/sources/refresh");
      await loadSkills();
      const failed = Object.entries(res?.failures || {});
      if (failed.length) {
        // honest badge: a failed fetch must never show a green ✓
        setSkillsMsg("");
        setPageErr("skill source fetch failed for " +
          failed.map(([n, r]) => `${n} (${r})`).join("; "));
        return;
      }
      setSkillsMsg("✓ external skills fetched");
      clearTimeout(skillsMsgTimer.current);
      skillsMsgTimer.current = setTimeout(() => setSkillsMsg(""), 4000);
    } catch (e) {
      setPageErr(`skill source fetch failed: ${String(e.message || e)}`);
    }
  };
  const [addSkill, setAddSkill] = useState(false);
  const [viewSkill, setViewSkill] = useState(null);
  // Remember deep-link intent across the async /skills load so we can open
  // only when the store is enabled, and clear the flag either way.
  const [pendingAdd, setPendingAdd] = useState(() => hashWantsAddSkill());
  useEffect(() => {
    const onHash = () => {
      if (hashWantsAddSkill()) setPendingAdd(true);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => {
    if (!pendingAdd) return;
    if (skillsErr) {
      clearAddSkillFlag();
      setPendingAdd(false);
      return;
    }
    // Wait until store status is known (null = still loading).
    if (skillsCatalog.store == null) return;
    clearAddSkillFlag();
    if (skillsCatalog.store.enabled) setAddSkill(true);
    setPendingAdd(false);
  }, [pendingAdd, skillsCatalog.store, skillsErr]);

  return (
    <>
      <Section id="skills" title="Skills"
        description="Reusable expertise installed into the agent session. External skill repositories are connected under Connections → Skill sources."
        help="Domain skill modules Devs may consult. Attach per Dev Type as Available (optional) or Required (soft-force prompt). Skills must be domain-specific and additive — never mission-step scripts."
        actions={
          <>
            <ImmediateBadge text="add/delete/restore apply immediately" />
            {skillsCatalog.store?.enabled && (
              <Button icon={Plus} onClick={() => setAddSkill(true)}>
                Add skill
              </Button>
            )}
            {(() => {
              // The catalog's secondary actions: store actions when the
              // store is enabled, PLUS the external-source fetch whenever
              // sources are connected (independent of the store — external
              // rows render on store-disabled stacks too). DESIGN §3: a
              // lone action stays a visible ghost button; 2+ fold into ⋯.
              const items = [];
              if (skillsCatalog.store?.enabled && skillsCatalog.store?.html_url) {
                items.push({
                  label: "Open the store in Gitea",
                  desc: "Skills live in a Git repo — edit them there directly.",
                  external: true,
                  onClick: () => window.open(skillsCatalog.store.html_url, "_blank", "noopener"),
                });
              }
              if (skillsCatalog.store?.enabled) {
                items.push({
                  label: "Restore built-in skills",
                  desc: "Re-adds any missing bundled skills. Never overwrites your edits.",
                  onClick: restoreBuiltins,
                });
              }
              if (skillSources.length) {
                items.push({
                  label: "Fetch skills from external sources",
                  desc: "Re-syncs every connected skill source's mirror and reloads the catalog. The same fetch runs automatically before any run that uses them.",
                  onClick: fetchExternal,
                });
              }
              if (!items.length) return null;
              if (items.length === 1) {
                return (
                  <Button kind="ghost" onClick={items[0].onClick}>
                    {items[0].label}
                  </Button>
                );
              }
              return <MoreMenu label="More skill-store actions" items={items} />;
            })()}
            <a href="#/skill-sources"
              className="text-xs font-medium text-accent-700 underline underline-offset-2 dark:text-accent-300">
              Skill sources
            </a>
          </>
        }>
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


      <ConfirmDialog open={!!confirm} {...(confirm || {})}
        onConfirm={() => confirm.action()}
        onCancel={() => setConfirm(null)} />
      {addSkill && <AddSkillDialog
        onClose={() => setAddSkill(false)} onSaved={loadSkills} />}
      {viewSkill && (
        <ViewSkillDialog name={viewSkill}
          skillSources={skillSources} repos={repos}
          onSaved={loadSkills}
          onClose={() => setViewSkill(null)} />
      )}
    </>
  );
}
