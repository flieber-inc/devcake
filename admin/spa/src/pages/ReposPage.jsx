import React, { useEffect, useState } from "react";
import { Trash2 } from "lucide-react";
import { get, send } from "../api.js";
import PageHeader from "../components/PageHeader.jsx";
import { Section } from "../components/Card.jsx";
import { Field, SecretField, Input, Select } from "../components/Field.jsx";
import SettingRow from "../components/SettingRow.jsx";
import Button from "../components/Button.jsx";
import Toggle from "../components/Toggle.jsx";
import { ConfirmDialog, Modal } from "../components/Modal.jsx";
import ImmediateBadge from "../components/ImmediateBadge.jsx";
import { AUTO_MERGE_COPY } from "../lib/configLabels.js";
import { getRegistry, loadRegistry } from "../lib/registry.js";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";
import { nextFreeName, useNewNames } from "../lib/instanceNames.js";

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
        <table className="w-full text-sm">
          <thead className="text-left text-neutral-500">
            <tr><th className="py-1 pr-3">Mission</th><th className="pr-3">Repo</th>
              <th className="pr-3">Size</th><th className="pr-3">Open PRs</th><th></th></tr>
          </thead>
          <tbody>
            {data.repos.map((r) => (
              <tr key={r.name} className="border-t border-neutral-200 dark:border-neutral-800">
                <td className="py-1.5 pr-3 font-mono">{r.mission_key}</td>
                <td className="pr-3">
                  <a className="text-blue-600 hover:underline" href={r.html_url}
                    target="_blank" rel="noreferrer">{r.name}</a>
                </td>
                <td className="pr-3">{Math.round(r.size_kb)} KB</td>
                <td className="pr-3">{r.open_prs}</td>
                <td className="text-right">
                  <Button kind="danger-ghost" icon={Trash2}
                    onClick={() => onClear(r.name)}>Clear</Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      )}
      {data.ui_url && (
        <a className="mt-2 inline-block text-sm text-blue-600 hover:underline"
          href={data.ui_url} target="_blank" rel="noreferrer">Open the Gitea UI →</a>
      )}
    </Section>
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
    <Modal ariaLabel="Create repository on the internal Gitea" onClose={busy ? undefined : onClose}>
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
      <div className="space-y-3">
        <Field label="Repository name"
          hint="Lowercase letters/digits, ≤12 — it becomes the card name too.">
          <Input value={name} onChange={(e) => setName(e.target.value)}
            placeholder="e.g. notes" />
        </Field>
        {err && <p className="text-sm text-red-600 dark:text-red-400">⚠ {err}</p>}
        <div className="flex flex-wrap justify-end gap-2">
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
// treats it as OK, so the note informs rather than warns
function RoOnlyNote({ name }) {
  const [state, setState] = useState(null);
  useEffect(() => {
    get(`/secrets-check?conn=repo:${name}:token,repo:${name}:token_ro`)
      .then((r) => setState({
        w: r.conn[`repo:${name}:token`]?.present,
        ro: r.conn[`repo:${name}:token_ro`]?.present,
      })).catch(() => setState(null));
  }, [name]);
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

export default function ReposPage() {
  const { dr, loadErr } = useSharedDraft();
  const [registry, setRegistry] = useState(getRegistry());
  useEffect(() => { loadRegistry().then(setRegistry); }, []);
  const [confirm, setConfirm] = useState(null);
  const [testResult, setTestResult] = useState({});
  const [clearErr, setClearErr] = useState("");
  const [internalRefresh, setInternalRefresh] = useState(0);
  const [giteaVariant, setGiteaVariant] = useState({});   // card idx → variant
  const [createFor, setCreateFor] = useState(null);       // card idx | null
  // cards added/renamed this session stay name-editable even when their name
  // collides with a still-saved one (the delete-then-re-add / mid-typing trap)
  const newNames = useNewNames(dr.server?.cfg.repos, dr.draft?.cfg.repos);

  if (!dr.loaded) {
    return <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading…{loadErr}</p>;
  }

  const cfg = dr.draft.cfg;
  const setField = dr.setField;
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

  const testForge = async (name) =>
    setTestResult({ ...testResult,
                    [`forge:${name}`]: await send("POST", `/connections/forge/${name}/test`) });

  return (
    <div className="space-y-5">
      <PageHeader title="Repositories"
        subtitle="Forge connections, tokens, merge policy, and the internal forge — edits apply on Save" />

      <Section id="repository" title="Repositories"
        description="Forge connections, access tokens and merge policy. Missions route to a repo via a `devcake-repo:<name>` line in their description, else the PMO instance's default repo; unrouted missions wait.">
        {cfg.repos.map((repo, idx) => {
          const tr = testResult[`forge:${repo.name}`];
          return (
            <div key={idx} className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-mono text-sm font-semibold">{repo.name || "(unnamed)"}</span>
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
                      });
                    };
                    const refPmos = cfg.pmos
                      .filter((p) => (p.repos || []).includes(repo.name)
                                  || (p.reference_repos || []).includes(repo.name))
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
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
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
                  help="This repo's forge token (repo read/write + PR scopes). Optional — with only a read-only token the repo serves as reference material. Stored securely — never echoed, never in .env."
                  refKey={`repo:${repo.name}:token`} paste
                  absentNote="not set — repo is reference-only until an Access token is stored"
                  locked={!nameLocked(repo.name, idx)} />
                <SecretField label="Read-only token" hint="Optional → clone-only for PLAN/REVIEW/ONBOARD"
                  help="Optional read-only token used by non-EXECUTE stages so a prompt-injected Dev can't push. Leave empty to give every stage the write token."
                  refKey={`repo:${repo.name}:token_ro`} paste optional
                  locked={!nameLocked(repo.name, idx)} />
                <SecretField label="Reviewer token" hint="Optional 2nd account → formal PR approvals"
                  help="Optional second account's token. When set, REVIEW posts a formal approval from that account before merging."
                  refKey={`repo:${repo.name}:reviewer_token`} paste optional
                  locked={!nameLocked(repo.name, idx)} />
              </div>
              {savedRepoNames.has(repo.name) && <RoOnlyNote name={repo.name} />}
              <div className="flex flex-wrap items-center gap-3">
                <Button kind="ghost" onClick={() => testForge(repo.name)}>Test connection</Button>
                <ImmediateBadge text="tests saved values" />
                {tr && (
                  <span className={`text-sm ${tr.ok ? "text-green-700 dark:text-green-400" : "text-red-600"}`}>
                    {tr.ok
                      ? tr.reference_only
                        ? `✓ ${tr.forge} reachable (read-only) — reference-only repo`
                        : `✓ ${tr.forge} reachable · reviewer token: ${tr.reviewer_token_configured ? "yes" : "no"}`
                      : `✗ ${tr.error}`}
                  </span>
                )}
              </div>
            </div>
          );
        })}
        <Button kind="ghost" onClick={() => {
          const name = nextFreeName("repo", cfg.repos, dr.server.cfg.repos);
          newNames.track(name);
          setField("cfg.repos", [...cfg.repos,
            { name, forge: "github", url: "",
              api_base: null, default_branch: "main" }]);
        }}>
          + Add repository
        </Button>
        <div className="divide-y divide-neutral-100 border-t border-neutral-100 dark:divide-neutral-800 dark:border-neutral-800">
          <SettingRow label="Auto-merge"
            desc={cfg.auto_merge
              ? "ON — approved PRs merge themselves (squash)."
              : "OFF — every merge is yours (DEVCAKE-MERGE handoff)."}
            help="ON: after DevCake's REVIEW step approves a PR, it merges itself (squash). OFF: DevCake stops at DEVCAKE-MERGE and waits for you to merge.">
            <Toggle on={cfg.auto_merge} label="Auto-merge"
              onClick={() =>
                cfg.auto_merge
                  ? setField("cfg.auto_merge", false)
                  : guardedFlip("cfg.auto_merge", true, "Merge without human review?",
                      AUTO_MERGE_COPY + "\n\n(Drafted now; applies when you Save.)")} />
          </SettingRow>
          {/* dependent rows: only meaningful while auto-merge is ON (drafted value) */}
          <div className={cfg.auto_merge ? "" : "opacity-50"} aria-disabled={!cfg.auto_merge}>
            <SettingRow label="Auto-resolve merge conflicts"
              desc={cfg.auto_resolve_merge_conflicts
                ? "ON — conflicts go back to EXECUTE (max 2 tries)."
                : "OFF — conflicts wait for you at DEVCAKE-MERGE."}
              help="Only applies when auto-merge is ON. When a merge fails on conflicts, DevCake sends the mission back to EXECUTE to sync the branch and resolve them (max 2 attempts) instead of waiting for you at DEVCAKE-MERGE.">
              <Toggle on={cfg.auto_resolve_merge_conflicts} label="Auto-resolve merge conflicts"
                disabled={!cfg.auto_merge}
                onClick={() => cfg.auto_merge &&
                  setField("cfg.auto_resolve_merge_conflicts", !cfg.auto_resolve_merge_conflicts)} />
            </SettingRow>
            <SettingRow label="Merge retry window"
              desc="Minutes to keep retrying a not-yet-mergeable PR before handing off."
              help="When a merge isn't possible yet (CI running, mergeability computing), DevCake keeps retrying via the merge sweep for this long before handing off with DEVCAKE-MERGE. Lower it on CI-light repos; raise it on CI-heavy repos. 0 = hand off immediately.">
              <Input type="number" className="w-24" min="0"
                disabled={!cfg.auto_merge}
                aria-label="Merge retry window (minutes)"
                value={cfg.merge_retry_window_minutes}
                onChange={(e) => setField("cfg.merge_retry_window_minutes",
                  Math.max(0, Number(e.target.value)))} />
            </SettingRow>
          </div>
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
    </div>
  );
}
