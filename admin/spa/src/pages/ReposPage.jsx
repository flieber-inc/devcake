import React, { useEffect, useRef, useState } from "react";
import { Trash2 } from "lucide-react";
import { get, send } from "../api.js";
import PageHeader from "../components/PageHeader.jsx";
import AdapterTabs from "../components/AdapterTabs.jsx";
import { Section } from "../components/Card.jsx";
import { Field, SecretField, Input, Select } from "../components/Field.jsx";
import SettingRow from "../components/SettingRow.jsx";
import Button from "../components/Button.jsx";
import Toggle from "../components/Toggle.jsx";
import { ConfirmDialog, Modal } from "../components/Modal.jsx";
import ImmediateBadge from "../components/ImmediateBadge.jsx";
import MoreMenu from "../components/MoreMenu.jsx";
import ClearSecretsDialog, { CLEAR_SECRETS_ENTRY } from "../components/ClearSecretsDialog.jsx";
import { AUTO_MERGE_COPY } from "../lib/configLabels.js";
import { getRegistry, loadRegistry } from "../lib/registry.js";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";
import { nextFreeName, useNewNames } from "../lib/instanceNames.js";
import { newRepoCard } from "../lib/cards.js";
import { unusedRepoNames } from "../lib/unusedRepos.js";
import { fleetSeedIndexes } from "../lib/fleetExpand.js";

// Repositories page (v0.1.1 B4, founder request): the repository cards +
// merge policy lifted out of Configuration, plus the internal-forge
// inventory — repository state in one place. Edits ride the SAME unified
// draft as Configuration (DraftChrome owns Save/DirtyBar/NavGuard).

// Read-only internal-forge repos (M11, founder decision: retain-by-default +
// a manual Clear). Hidden entirely when the internal forge is disabled.
function InternalReposSection({ onClear, onClearAll, refreshKey }) {
  const [data, setData] = useState(null);
  const load = () => get("/internal-repos").then(setData).catch(() => setData({ repos: [] }));
  useEffect(() => { load(); }, [refreshKey]);
  // hidden only when the internal forge is DISABLED (no ui_url); an empty
  // repo list keeps the section — and its Gitea UI link — visible
  if (!data || (!data.ui_url && data.repos.length === 0)) return null;
  return (
    <Section id="internal-forge" title="Internal forge"
      description="Repositories DevCake auto-created for missions with no configured repo. Retained until you clear them; the deliverable is already in the PMO feed."
      actions={data.repos.length > 0 && (
        <Button kind="danger-ghost" icon={Trash2}
          onClick={() => onClearAll(data.repos.map((r) => r.name))}>
          Clear data
        </Button>
      )}>
      {data.repos.length === 0 && (
        <p className="text-sm text-neutral-500 dark:text-neutral-400">
          No internal repos right now — a mission that resolves to no
          configured repository creates one automatically.
        </p>
      )}
      {data.repos.length > 0 && (
      <div className="overflow-x-auto">
        <table className="w-full min-w-[32rem] text-sm">
          <thead className="text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
            <tr>
              <th className="py-1 pr-3 text-left font-medium">Mission</th>
              <th className="pr-3 text-left font-medium">Repo</th>
              <th className="pr-3 text-left font-medium">Size</th>
              <th className="pr-3 text-left font-medium">Open PRs</th>
              <th className="text-right font-medium"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {data.repos.map((r) => (
              <tr key={r.name}
                className="border-t border-neutral-100 hover:bg-stone-50 dark:border-neutral-800 dark:hover:bg-neutral-900">
                <td className="py-1.5 pr-3 font-mono text-xs">{r.mission_key}</td>
                <td className="pr-3">
                  <a className="text-accent-600 hover:underline dark:text-accent-300" href={r.html_url}
                    target="_blank" rel="noreferrer">{r.name}</a>
                </td>
                <td className="pr-3 tabular-nums">{Math.round(r.size_kb)} KB</td>
                <td className="pr-3 tabular-nums">{r.open_prs}</td>
                <td className="text-right">
                  <Button kind="danger-ghost" size="sm" icon={Trash2}
                    onClick={() => onClear(r.name)}>Clear data</Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      )}
      {data.ui_url && (
        <a className="mt-2 inline-block text-sm text-accent-600 hover:underline"
          href={data.ui_url} target="_blank" rel="noreferrer">Open the Gitea UI →</a>
      )}
    </Section>
  );
}

function RepoUsageChips({ name, cfg, devTypes, depth }) {
  const work = (cfg.pmos || []).filter((p) => (p.repos || []).includes(name)).length;
  const ref = (cfg.pmos || []).filter((p) => (p.reference_repos || []).includes(name)).length;
  const memB = (cfg.pmos || []).filter((p) => (p.memory_repos || []).includes(name)).length;
  const memD = Object.values(devTypes || {}).filter((d) => (d.memory_repos || []).includes(name)).length;
  const skills = Object.values(devTypes || {}).some((d) =>
    (d.skills || []).some((s) => s.startsWith(`${name}/`)));
  const chips = [];
  if (work) chips.push(`work ×${work}`);
  if (ref) chips.push(`reference ×${ref}`);
  if (skills) chips.push("skills-source");
  if (memB) chips.push(`memory board-bound ×${memB}`);
  if (memD) chips.push(`memory domain-bound ×${memD}`);
  if (typeof depth === "number") chips.push(`queue ${depth}`);
  if (!chips.length) return null;
  return (
    <span className="flex flex-wrap gap-1">
      {chips.map((c) => (
        <span key={c}
          className="rounded bg-stone-100 px-1.5 py-0.5 text-[10px] font-medium text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
          {c}
        </span>
      ))}
    </span>
  );
}

// "gitea (internal)" is a UI variant, not a forge id — both map to
// forge:"gitea"; internal is recognized by the bundled instance's clone
// origin in the URL. Selecting internal offers Create repository.
const INTERNAL_ORIGIN_MARK = "://gitea:";
const giteaVariantOf = (repo) =>
  repo.url && repo.url.includes(INTERNAL_ORIGIN_MARK)
    ? "gitea-internal" : "gitea-external";

function CreateInternalRepoModal({ initialName, onClose, onCreated }) {
  const [name, setName] = useState(initialName || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const create = async () => {
    if (name.startsWith("activity-")) {
      setErr("Name must not start with activity- (those repos are swept on Clear).");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const out = await send("POST", "/internal-repos/create", { name });
      onCreated(out);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal onClose={busy ? undefined : onClose}>
      <h4 className="mb-1 text-base font-semibold tracking-tight">
        Create repository on the internal Gitea
      </h4>
      <p className="mb-3 text-sm text-neutral-500 dark:text-neutral-400">
        Creates a private repo (org <code className="font-mono text-xs">devcake-repos</code>,
        protected main) and stores its access/read-only/reviewer tokens for
        this card automatically — save the page afterwards and the card is
        fully wired. If the repo already exists in that org it is adopted
        instead: fresh tokens are minted and stored (the recovery path after
        removing a card, which deletes its stored tokens).
      </p>
      <p className="mb-3 text-sm text-neutral-600 dark:text-neutral-300">
        This repository is yours. Clear will not delete it. A full
        stack wipe will, unless you use the Gitea backup. After
        Clear, claims copied from the wiped board are removed from
        this notebook; notes stay.
      </p>
      <div className="space-y-3">
        <Field label="Repository name"
          hint="Lowercase letters/digits, ≤12 — it becomes the card name too.">
          <Input value={name} onChange={(e) => setName(e.target.value)}
            placeholder="e.g. notes" />
        </Field>
        {err && <p className="text-sm text-red-600 dark:text-red-400">⚠ {err}</p>}
        <div className="flex justify-end gap-2">
          <Button kind="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
          <Button disabled={busy || !name} onClick={create}>
            {busy ? "Creating…" : "Create repository"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// neutral state note when a saved repo stores ONLY a read-only token: a
// first-class reference-only repo (founder decisions 2026-07-15) — health
// treats it as OK, so the note informs rather than warns. Reads the page's
// batched presence map (bulk-scale 2026-08-02) instead of its own fetch.
function RoOnlyNote({ name, presence }) {
  const state = presence && {
    w: presence[`repo:${name}:token`]?.present,
    ro: presence[`repo:${name}:token_ro`]?.present,
  };
  if (!state || state.w || !state.ro) return null;
  return (
    <p className="text-xs text-neutral-500 dark:text-neutral-400">
      ℹ Reference-only repo (read-only token, no Access token): select it
      under a PMO's Reference repos — every stage gets a read-only clone as
      consultation material. It is never a work target; add an Access token
      later to make it workable.
    </p>
  );
}

export default function ReposPage({ onHealthChange }) {
  const { dr, loadErr, reload, repoNewNamesState, healthInfo } = useSharedDraft();
  const [registry, setRegistry] = useState(getRegistry());
  useEffect(() => { loadRegistry().then(setRegistry); }, []);
  const [confirm, setConfirm] = useState(null);
  const [testResult, setTestResult] = useState({});
  const [clearErr, setClearErr] = useState("");
  const [internalRefresh, setInternalRefresh] = useState(0);
  const [giteaVariant, setGiteaVariant] = useState({});   // card idx → variant
  const [createFor, setCreateFor] = useState(null);       // card idx | null
  const [clearSecrets, setClearSecrets] = useState(false);
  const [secretsEpoch, setSecretsEpoch] = useState(0);
  // cards added/renamed this session stay name-editable even when their name
  // collides with a still-saved one (the delete-then-re-add / mid-typing trap)
  // the Set lives in the provider (2026-08-02): an internal Set was lost on
  // nav-away, so a session-added card whose name collided with a still-saved
  // one came back born name-locked
  const newNames = useNewNames(dr.server?.cfg.repos, dr.draft?.cfg.repos,
                               repoNewNamesState);
  // bulk-scale (2026-08-02): 350 cards × (3 SecretField self-fetches + a
  // RoOnlyNote fetch) was ~1,400 requests per page load. The page renders at
  // most CARD_CAP filtered cards, and ONE chunked batch covers their three
  // token fields; SecretFields receive it via the `presence` prop and skip
  // their self-fetch. The filter is debounced so typing doesn't refetch per
  // keystroke.
  const CARD_CAP = 30;
  const [filter, setFilter] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setQuery(filter), 300);
    return () => clearTimeout(t);
  }, [filter]);
  const [presence, setPresence] = useState({});   // "repo:name:field" → {present}
  const draftRepoNames = (dr.draft?.cfg.repos || []).map((r) => r.name);
  // filter + cap engage only past CARD_CAP — small fleets keep every card,
  // and a leftover filter can never hide cards while its input is hidden.
  // 2026-08 reviewer round: the input now appears past 5 repos (it used to
  // wait for the 30-card cap); the hard render cap stays at CARD_CAP.
  const filterActive = draftRepoNames.length > 5;
  const fq = filterActive ? query.trim().toLowerCase() : "";
  const matchedNames = filterActive
    ? draftRepoNames.filter((n) => !fq || n.toLowerCase().includes(fq))
    : draftRepoNames;
  const visibleNames = draftRepoNames.length > CARD_CAP
    ? matchedNames.slice(0, CARD_CAP)
    : matchedNames;
  const visibleKey = visibleNames.join(",");

  // 2026-08 reviewer round: collapsed summary rows — one ~350px card per
  // repo meant 2-3 fit a screen. Each card collapses to a one-line summary
  // (name · forge · URL host · merge posture); expansion is per-operator
  // ephemeral view state. ≤3 repos start expanded; a newly added repo opens
  // expanded (its form needs filling).
  // (keyed by INDEX, not name — a rename mid-edit must not collapse the
  // card being typed in; indexes only shift on the rare remove)
  // Seed ONCE after the draft loads: useState often runs with length 0
  // (draft still empty), which would leave small fleets collapsed.
  const [expandedRepos, setExpandedRepos] = useState(() => new Set());
  const repoExpandSeeded = useRef(false);
  useEffect(() => {
    const indexes = fleetSeedIndexes(draftRepoNames.length, repoExpandSeeded.current);
    if (indexes === null) return;
    repoExpandSeeded.current = true;
    setExpandedRepos(new Set(indexes));
  }, [draftRepoNames.length]);
  const toggleRepoCard = (i) => setExpandedRepos((prev) => {
    const next = new Set(prev);
    if (next.has(i)) next.delete(i); else next.add(i);
    return next;
  });
  useEffect(() => {
    const refs = visibleNames.filter(Boolean).flatMap((n) =>
      [`repo:${n}:token`, `repo:${n}:token_ro`, `repo:${n}:reviewer_token`]);
    if (!refs.length) return;
    let live = true;
    const chunks = [];
    for (let i = 0; i < refs.length; i += 40) chunks.push(refs.slice(i, i + 40));
    Promise.all(chunks.map((chunk) =>
      get(`/secrets-check?conn=${encodeURIComponent(chunk.join(","))}`)
        .then((r) => Object.fromEntries(chunk.map((ref) => [ref, r.conn[ref] || { present: false }])))
        .catch(() => ({}))))
      .then((parts) => { if (live) setPresence((p) => Object.assign({}, p, ...parts)); });
    return () => { live = false; };
  }, [visibleKey, secretsEpoch]);

  if (!dr.loaded) {
    return <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading…{loadErr}</p>;
  }

  const cfg = dr.draft.cfg;
  const setField = dr.setField;
  const visibleSet = new Set(visibleNames);
  const shownRepos = cfg.repos.filter((r) => visibleSet.has(r.name));
  const hiddenCount = cfg.repos.length - shownRepos.length;
  // stored tokens key on the repo name — locked once saved. A card counts as
  // saved only when the server holds its name AND it isn't the card that was
  // (re)added this session, so a new card can never be born frozen. When a
  // new card duplicates a saved name (blocked by validation anyway), only the
  // LAST card with that name is the tracked one — the saved card stays locked.
  const savedRepoNames = new Set((dr.server.cfg.repos || []).map((r) => r.name));
  const nameLocked = (name, idx) =>
    savedRepoNames.has(name) &&
    !(newNames.has(name) && idx === cfg.repos.map((r) => r.name).lastIndexOf(name));

  const guardedFlip = (path, value, title, body) =>
    setConfirm({
      title, body, confirmLabel: "I understand — proceed",
      action: () => { setField(path, value); setConfirm(null); },
    });

  // computed from the DRAFT, so unsaved PMO (de)selections count. Predicate
  // matches /health.unused_repos (work + reference + memory; no skills).
  const removeUnusedRepos = () => {
    const names = unusedRepoNames(cfg, dr.draft.devTypes || {});
    if (names.length === 0) {
      setConfirm({
        title: "No unused repositories",
        body: "Every configured repository is selected as a work, reference, or memory repo on a board or Dev Type. Skill sources are managed separately under Configuration → Skills.",
        confirmLabel: "OK",
        action: () => setConfirm(null),
      });
      return;
    }
    const shown = names.slice(0, 8).join(", ");
    const more = names.length > 8 ? ` and ${names.length - 8} more` : "";
    setConfirm({
      title: `Remove ${names.length} unused repositor${names.length > 1 ? "ies" : "y"}?`,
      body: `${shown}${more} — selected as work, reference, or memory on no board or Dev Type. `
        + "Skill sources are a separate connection type and are not repo cards. "
        + "Removing them and saving permanently deletes their stored tokens "
        + "(write / read-only / reviewer); a run still in flight on one fails "
        + "cleanly. Nothing changes until you Save.",
      confirmLabel: "Remove from draft",
      danger: true,
      action: () => {
        names.forEach((n) => newNames.untrack(n));
        setField("cfg.repos", cfg.repos.filter((r) => !names.includes(r.name)));
        setConfirm(null);
      },
    });
  };

  const testForge = async (name) =>
    setTestResult({ ...testResult,
                    [`forge:${name}`]: await send("POST", `/connections/forge/${name}/test`) });

  return (
    <div className="space-y-5">
      <PageHeader title="Repositories"
        subtitle="Forge connections, tokens, merge policy, and the internal forge — edits apply on Save" />
      <AdapterTabs page="repos" />

      <Section id="repository" title="Repositories"
        description="Forge connections, access tokens and merge policy. Missions route to a repo via a `devcake-repo:<name>` line in their description, else the PMO instance's default repo; unrouted missions wait."
        actions={
          <MoreMenu label="More repository actions" items={[
            { label: "Remove unused repositories…", danger: true,
              desc: "Drop every repo no board or Dev Type selects as work, reference, or memory — their stored tokens are deleted on Save.",
              onClick: removeUnusedRepos },
            { label: CLEAR_SECRETS_ENTRY.menuLabel, danger: true,
              desc: CLEAR_SECRETS_ENTRY.desc,
              onClick: () => setClearSecrets(true) },
          ]} />
        }>
        {filterActive && (
          <div className="flex flex-wrap items-center gap-3">
            <span className="relative w-64 shrink-0">
              <Input className="pr-7" value={filter}
                placeholder={`Filter ${cfg.repos.length} repositories…`}
                aria-label="Filter repositories by name"
                onChange={(e) => setFilter(e.target.value)} />
              {filter && (
                <button type="button" aria-label="Clear repository filter"
                  onClick={() => setFilter("")}
                  className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-neutral-500 hover:text-neutral-800 dark:text-neutral-400 dark:hover:text-neutral-100">
                  ✕
                </button>
              )}
            </span>
            {hiddenCount > 0 && (
              <span className="text-xs text-neutral-500 dark:text-neutral-400">
                Showing {shownRepos.length} of {cfg.repos.length} — refine the filter
              </span>
            )}
          </div>
        )}
        {cfg.repos.map((repo, idx) => {
          if (!visibleSet.has(repo.name)) return null;
          const tr = testResult[`forge:${repo.name}`];
          if (!expandedRepos.has(idx)) {
            let host = "";
            try { host = repo.url ? new URL(repo.url).host : ""; } catch { host = ""; }
            return (
              <button key={`${idx}-${secretsEpoch}`} type="button"
                data-testid="repo-summary-row"
                aria-label={`Expand repository ${repo.name}`}
                onClick={() => toggleRepoCard(idx)}
                className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 rounded-card border border-neutral-200 px-4 py-2.5 text-left transition hover:bg-stone-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:border-neutral-800 dark:hover:bg-neutral-900">
                <span className="font-mono text-sm font-semibold">{repo.name || "(unnamed)"}</span>
                <span className="text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">{repo.forge}</span>
                {host && <span className="min-w-0 truncate text-xs text-neutral-500 dark:text-neutral-400">{host}</span>}
                <span className="ml-auto flex shrink-0 items-center gap-3">
                  <span className="text-[11px] text-neutral-400 dark:text-neutral-500">
                    {repo.auto_merge ? "auto-merge" : "merge hand-off"}
                  </span>
                  {tr && (
                    <span className={`text-xs ${tr.ok ? "text-green-700 dark:text-green-400" : "text-red-600"}`}>
                      {tr.ok ? "✓" : "✗"}
                    </span>
                  )}
                  <span aria-hidden className="text-xs text-neutral-400">▸</span>
                </span>
              </button>
            );
          }
          return (
            <div key={`${idx}-${secretsEpoch}`} className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="flex items-center gap-2">
                  <button type="button"
                    aria-label={`Collapse repository ${repo.name}`}
                    title="Collapse to a summary row"
                    onClick={() => toggleRepoCard(idx)}
                    className="rounded text-xs text-neutral-400 hover:text-neutral-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:text-neutral-500 dark:hover:text-neutral-300">
                    ▾
                  </button>
                  <span className="font-mono text-sm font-semibold">{repo.name || "(unnamed)"}</span>
                  <RepoUsageChips name={repo.name} cfg={cfg}
                    devTypes={dr.draft.devTypes}
                    depth={(healthInfo?.claims_depth || {})[repo.name]} />
                </span>
                {cfg.repos.length > 0 && (
                  <Button kind="danger-ghost" onClick={() => {
                    const doRemove = () => {
                      newNames.untrack(repo.name);
                      setField("cfg.repos", cfg.repos.filter((_, i) => i !== idx));
                      // cascade: the server refuses a PMO that still lists a
                      // repo name with no card — deselect it everywhere too
                      cfg.pmos.forEach((p, pi) => {
                        if ((p.repos || []).includes(repo.name))
                          setField(`cfg.pmos.${pi}.repos`,
                            p.repos.filter((n) => n !== repo.name));
                        if ((p.reference_repos || []).includes(repo.name))
                          setField(`cfg.pmos.${pi}.reference_repos`,
                            p.reference_repos.filter((n) => n !== repo.name));
                        if ((p.memory_repos || []).includes(repo.name))
                          setField(`cfg.pmos.${pi}.memory_repos`,
                            p.memory_repos.filter((n) => n !== repo.name));
                      });
                      Object.entries(dr.draft.devTypes || {}).forEach(([nm, dt]) => {
                        if ((dt.memory_repos || []).includes(repo.name))
                          setField(`devTypes.${nm}.memory_repos`,
                            dt.memory_repos.filter((n) => n !== repo.name));
                      });
                    };
                    const refPmos = cfg.pmos
                      .filter((p) => (p.repos || []).includes(repo.name)
                                  || (p.reference_repos || []).includes(repo.name)
                                  || (p.memory_repos || []).includes(repo.name))
                      .map((p) => p.name);
                    if (savedRepoNames.has(repo.name)) {
                      setConfirm({
                        title: `Remove repository "${repo.name}"?`,
                        body: "Removing it and saving permanently deletes its stored tokens (write / read-only / reviewer); missions that used this repo gate until a human closes them out."
                          + (refPmos.length
                              ? ` It is also deselected from PMO connection${refPmos.length > 1 ? "s" : ""} ${refPmos.join(", ")}.`
                              : "")
                          + " Nothing changes until you Save.",
                        confirmLabel: "Remove from draft",
                        action: () => { doRemove(); setConfirm(null); },
                      });
                    } else doRemove();
                  }}>
                    Remove
                  </Button>
                )}
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <Field label="Repo name"
                  help="Operator-chosen identity (lowercase letters/digits, ≤12, no hyphens). Missions reference it in `devcake-repo:` markers and PMO default-repo settings. Locked once saved — stored tokens key on it; remove and re-add to rename.">
                  <Input value={repo.name} disabled={nameLocked(repo.name, idx)}
                  onChange={(e) => {
                    newNames.rename(repo.name, e.target.value);
                    setField(`cfg.repos.${idx}.name`, e.target.value);
                  }} />
                  {dr.errors[`cfg.repos.${idx}.name`] && (
                    <span className="mt-1 block text-xs text-red-600 dark:text-red-400">
                      ✗ {dr.errors[`cfg.repos.${idx}.name`]}
                    </span>
                  )}</Field>
                <Field label="Forge"
                  help="Where the repository lives. Selects the API DevCake uses for pull/merge requests, approvals and merges. 'gitea (internal)' targets the always-on bundled forge — Create repository mints the repo and its tokens for you.">
                  <Select
                    value={repo.forge === "gitea"
                      ? (giteaVariant[idx] ?? giteaVariantOf(repo))
                      : repo.forge}
                    onChange={(e) => {
                      const v = e.target.value;
                      if (v === "gitea-internal" || v === "gitea-external") {
                        setField(`cfg.repos.${idx}.forge`, "gitea");
                        setGiteaVariant({ ...giteaVariant, [idx]: v });
                      } else {
                        setField(`cfg.repos.${idx}.forge`, v);
                        setGiteaVariant({ ...giteaVariant, [idx]: undefined });
                      }
                    }}>
                    {registry.forges.filter((f) => f.id !== "gitea").map((f) => (
                      <option key={f.id} value={f.id}>{f.id}</option>
                    ))}
                    <option value="gitea-external">gitea (external)</option>
                    <option value="gitea-internal">gitea (internal)</option>
                  </Select>
                </Field>
                {repo.forge === "gitea" &&
                  (giteaVariant[idx] ?? giteaVariantOf(repo)) === "gitea-internal" && (
                  <Field label="Internal repository"
                    help="Creates a repo on the bundled Gitea and stores this card's tokens automatically. If the URL is already filled, the repo exists.">
                    <Button kind="ghost" onClick={() => setCreateFor(idx)}>
                      + Create repository
                    </Button>
                  </Field>
                )}
                <Field label="Repository URL"
                  help="HTTPS URL of the repository, e.g. https://github.com/you/repo.git. Devs clone it; the app opens and merges PRs on it. Empty = repo stays idle.">
                  <Input value={repo.url}
                  onChange={(e) => setField(`cfg.repos.${idx}.url`, e.target.value)} /></Field>
                <SecretField label="Access token"
                  help="This repo's forge token (repo read/write + PR scopes). Optional — with only a read-only token the repo serves as reference material. Stored as plaintext mode 0600 on the app volume — never echoed, never in .env."
                  refKey={`repo:${repo.name}:token`} paste
                  absentNote="not set — repo is reference-only until an Access token is stored"
                  presence={presence[`repo:${repo.name}:token`] || null}
                  locked={!nameLocked(repo.name, idx)} />
                <SecretField label="Read-only token" hint="Optional → clone-only for PLAN/REVIEW/ONBOARD"
                  help="Optional read-only token used by non-EXECUTE stages so a prompt-injected Dev can't push with a write-capable PAT. Leave empty to give every stage the write token (and, if the default branch is unprotected, merge capability)."
                  refKey={`repo:${repo.name}:token_ro`} paste optional
                  presence={presence[`repo:${repo.name}:token_ro`] || null}
                  locked={!nameLocked(repo.name, idx)} />
                <SecretField label="Reviewer token" hint="Recommended 2nd account → formal PR approvals"
                  help="Recommended second account's token for formal forge approval under branch protection. The app (never a Dev) files the approval after the REVIEW stage judges the PR. Not a supply-chain control when you staff a different Dev Type for REVIEW — that is role focus only."
                  refKey={`repo:${repo.name}:reviewer_token`} paste optional
                  presence={presence[`repo:${repo.name}:reviewer_token`] || null}
                  locked={!nameLocked(repo.name, idx)} />
              </div>
              {savedRepoNames.has(repo.name) && <RoOnlyNote name={repo.name} presence={presence} />}
              <div className="flex flex-wrap items-center gap-3">
                <Button kind="ghost" onClick={() => testForge(repo.name)}>Test connection</Button>
                <ImmediateBadge text="tests saved values" />
                {tr && (
                  <span className={`text-sm ${tr.ok ? "text-green-700 dark:text-green-400" : "text-red-600"}`}>
                    {tr.ok
                      ? tr.reference_only
                        ? `✓ ${tr.forge} reachable (read-only) — reference-only repo`
                        : `✓ ${tr.forge} reachable · reviewer token: ${tr.reviewer_token_configured ? "yes" : "no"}`
                      : `✗ ${tr.error || tr.detail || "connection test failed"}`}
                  </span>
                )}
              </div>
              {/* per-repo merge doctrine (ADR-0020) — not a deployment master switch */}
              <div className="divide-y divide-neutral-100 border-t border-neutral-100 dark:divide-neutral-800 dark:border-neutral-800">
                <SettingRow label="Auto-merge"
                  desc={!!repo.auto_merge
                    ? "ON — the app merges approved PRs (squash)."
                    : "OFF — app will not merge (DEVCAKE-MERGE handoff)."}
                  help="ON: after REVIEW approves, the app squash-merges this repo. OFF: the app stops at DEVCAKE-MERGE and does not call merge — protect the default branch so Devs (who still hold write tokens) cannot merge either.">
                  <Toggle on={!!repo.auto_merge} label="Auto-merge"
                    onClick={() =>
                      repo.auto_merge
                        ? setField(`cfg.repos.${idx}.auto_merge`, false)
                        : guardedFlip(`cfg.repos.${idx}.auto_merge`, true,
                            `Merge "${repo.name || "this repo"}" without human review?`,
                            AUTO_MERGE_COPY + "\n\n(Drafted now; applies when you Save.)")} />
                </SettingRow>
                <div className={repo.auto_merge ? "" : "opacity-50"}
                  aria-disabled={!repo.auto_merge}>
                  <SettingRow label="Auto-resolve merge conflicts"
                    desc={(repo.auto_resolve_merge_conflicts ?? true)
                      ? "ON — conflicts go back to EXECUTE (max 2 tries)."
                      : "OFF — conflicts wait for you at DEVCAKE-MERGE."}
                    help="Only applies when auto-merge is ON for this repo. When a merge fails on conflicts, DevCake sends the mission back to EXECUTE to sync the branch and resolve them (max 2 attempts) instead of waiting for you at DEVCAKE-MERGE.">
                    <Toggle on={repo.auto_resolve_merge_conflicts ?? true}
                      label="Auto-resolve merge conflicts"
                      disabled={!repo.auto_merge}
                      onClick={() => repo.auto_merge &&
                        setField(`cfg.repos.${idx}.auto_resolve_merge_conflicts`,
                          !(repo.auto_resolve_merge_conflicts ?? true))} />
                  </SettingRow>
                  <SettingRow label="Post-approve settle"
                    desc="Minutes to wait after REVIEW approve before auto-merge, so sibling discoveries can land."
                    help="Only when auto-merge is ON. After approve, DevCake holds on DEVCAKE-MERGE for this long so routed discoveries from sibling missions can arrive; at the end of the window it re-checks freshness and may re-open REVIEW. 0 = merge as soon as the forge allows (today's behavior). Distinct from the merge retry window (CI / mergeability)." >
                    <Input type="number" className="w-24" min="0"
                      disabled={!repo.auto_merge}
                      aria-label="Post-approve settle (minutes)"
                      value={repo.merge_settle_minutes ?? 0}
                      onChange={(e) => setField(
                        `cfg.repos.${idx}.merge_settle_minutes`,
                        Math.max(0, Number(e.target.value)))} />
                  </SettingRow>
                  <SettingRow label="Merge retry window"
                    desc="Minutes to keep retrying a not-yet-mergeable PR before handing off."
                    help="When a merge isn't possible yet (CI running, mergeability computing), DevCake keeps retrying via the merge sweep for this long before handing off with DEVCAKE-MERGE. Lower it on CI-light repos; raise it on CI-heavy repos. 0 = hand off immediately.">
                    <Input type="number" className="w-24" min="0"
                      disabled={!repo.auto_merge}
                      aria-label="Merge retry window (minutes)"
                      value={repo.merge_retry_window_minutes ?? 30}
                      onChange={(e) => setField(
                        `cfg.repos.${idx}.merge_retry_window_minutes`,
                        Math.max(0, Number(e.target.value)))} />
                  </SettingRow>
                </div>
              </div>
            </div>
          );
        })}
        <Button kind="ghost" onClick={() => {
          const name = nextFreeName("repo", cfg.repos, dr.server.cfg.repos);
          newNames.track(name);
          setExpandedRepos((prev) => new Set(prev).add(cfg.repos.length));
          setField("cfg.repos", [...cfg.repos, newRepoCard(name)]);
        }}>
          + Add repository
        </Button>
        <div className="divide-y divide-neutral-100 border-t border-neutral-100 dark:divide-neutral-800 dark:border-neutral-800">
          <SettingRow label="Also attach merged change set to PMO"
            desc={cfg.attach_merged_changeset_to_pmo
              ? "ON — after merge, zip PR files onto the PMO feed (configured repos too)."
              : "OFF (recommended for eng repos) — only zero-repo / internal missions attach a zip."}
            help={"Zero-repo missions always attach a deliverable zip; this toggle only affects "
              + "configured work repos. Leave OFF for normal software work: the forge PR is "
              + "the canonical artifact. When ON, DevCake posts a merge-time file snapshot to "
              + "the PMO (can omit large files under the attachment size cap; may go stale vs "
              + "main; repo file bytes become visible to the PMO team). This does not pass work "
              + "into other missions' workspaces — dependency edges and blocker clones do that."}>
            <Toggle on={!!cfg.attach_merged_changeset_to_pmo}
              label="Also attach merged change set to PMO"
              onClick={() => setField("cfg.attach_merged_changeset_to_pmo",
                !cfg.attach_merged_changeset_to_pmo)} />
          </SettingRow>
        </div>
      </Section>

      {clearErr && <p className="text-sm text-red-600 dark:text-red-400">✗ {clearErr}</p>}
      <InternalReposSection refreshKey={internalRefresh}
        onClearAll={(names) => setConfirm({
          title: `Clear ALL internal-forge data (${names.length} repo${names.length > 1 ? "s" : ""})?`,
          body: "Deletes every auto-created repository, its machine user (revoking "
            + "its tokens), and the stored credentials. A repo currently used by a "
            + "live run is refused and kept. Deliverables already attached to the "
            + "PMO feed are untouched. This cannot be undone.",
          confirmLabel: `Delete ${names.length} internal repo${names.length > 1 ? "s" : ""}`,
          danger: true,
          action: async () => {
            setConfirm(null);
            setClearErr("");
            const failed = [];
            for (const name of names) {
              try { await send("DELETE", `/internal-repos/${name}`); }
              catch (e) { failed.push(`${name}: ${String(e.message || e)}`); }
            }
            if (failed.length) setClearErr(`kept ${failed.length}: ${failed.join(" · ")}`);
            setInternalRefresh((n) => n + 1);
          },
        })}
        onClear={(name) => setConfirm({
          title: `Clear internal repo '${name}'?`,
          body: "Deletes the auto-created repository, its machine user (revoking "
            + "its tokens), and the stored credentials. The deliverable already "
            + "attached to the PMO feed is untouched. This cannot be undone.",
          confirmLabel: "Delete the internal repo",
          danger: true,
          action: async () => {
            setConfirm(null);
            try {
              await send("DELETE", `/internal-repos/${name}`);
              setInternalRefresh((n) => n + 1);
            } catch (e) { setClearErr(String(e.message || e)); }
          },
        })} />

      {createFor !== null && (
        <CreateInternalRepoModal
          initialName={cfg.repos[createFor]?.name || ""}
          onClose={() => setCreateFor(null)}
          onCreated={(out) => {
            // the created repo name IS the card name (tokens are stored
            // under repo:{name}); the clone URL wires the card — Save
            // persists it, and secrets-check flips to ✓ stored
            newNames.rename(cfg.repos[createFor]?.name, out.repo_name);
            setField(`cfg.repos.${createFor}.name`, out.repo_name);
            setField(`cfg.repos.${createFor}.url`, out.clone_url);
            setCreateFor(null);
          }} />
      )}
      <ConfirmDialog open={!!confirm} {...(confirm || {})}
        onConfirm={() => confirm.action()}
        onCancel={() => setConfirm(null)} />
      {clearSecrets && (
        <ClearSecretsDialog
          context="repos"
          onClose={() => setClearSecrets(false)}
          onCleared={async (result) => {
            // Swallow reload errors so the ConfirmDialog does not re-label a
            // successful delete as a failed clear (Fable PR #54 review).
            try {
              setClearErr("");
              setTestResult({});
              await reload();
              setSecretsEpoch((e) => e + 1);
              if (result?.intake_paused) {
                onHealthChange?.((h) => ({ ...h, intake_paused: true }));
              }
            } catch (e) {
              setClearErr(
                `reload after clear secrets failed: ${String(e.message || e)}`);
            }
          }}
        />
      )}
    </div>
  );
}
