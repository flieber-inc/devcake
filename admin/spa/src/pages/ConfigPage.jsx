import React, { useEffect, useRef, useState } from "react";
import { Play, Plus, Trash2, KeyRound, Upload } from "lucide-react";
import { get, send } from "../api.js";
import PageHeader from "../components/PageHeader.jsx";
import { Section } from "../components/Card.jsx";
import { Field, Help, SecretField, Input, Select, Textarea } from "../components/Field.jsx";
import Button from "../components/Button.jsx";
import Toggle from "../components/Toggle.jsx";
import { ConfirmDialog } from "../components/Modal.jsx";
import ImmediateBadge from "../components/ImmediateBadge.jsx";
import DirtyBar from "../components/DirtyBar.jsx";
import SaveReviewDialog from "../components/SaveReviewDialog.jsx";
import NavGuardDialog from "../components/NavGuardDialog.jsx";
import useConfigDraft from "../lib/useConfigDraft.js";
import { describeDiff, AUTO_MERGE_COPY, ADOPTION_COPY } from "../lib/configLabels.js";
import { CONFIG_SECTIONS } from "../lib/nav.js";
import { getRegistry, loadRegistry } from "../lib/registry.js";

const MISSION_TYPES = ["ONBOARD", "PLAN", "EXECUTE", "REVIEW"];
// harness identities (image, credential requirements, OAuth availability) come
// from GET /harnesses — the registry is authoritative, nothing is hardcoded here

// ── OAuth wizard (docs/16 M6): device-code flow driven from the UI ──────────
// Immediate by nature: the credential is stored server-side the moment the
// device flow completes — it cannot wait for the page's Save.

function OAuthWizard({ devType, onClose }) {
  const [status, setStatus] = useState({ state: "starting" });
  const [runId, setRunId] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let timer;
    send("POST", `/oauth/dev-types/${devType}/start`)
      .then(({ run_id }) => {
        setRunId(run_id);
        timer = setInterval(
          () => get(`/oauth/status/${run_id}`).then(setStatus).catch(() =>
            setStatus({ state: "failed", error: "session lost (app restarted?) — close and retry" })),
          3000
        );
      })
      .catch((e) => setError(String(e)));
    return () => clearInterval(timer);
  }, [devType]);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-[2px]">
      <div className="w-full max-w-lg rounded-card border border-neutral-200 bg-white p-6 shadow-2xl dark:border-neutral-800 dark:bg-neutral-900">
        <h4 className="mb-3 text-base font-semibold tracking-tight">Connect {devType} (OAuth)</h4>
        {error && <p className="text-sm text-red-600">{error}</p>}
        {!error && status.state === "starting" && (
          <p className="text-sm text-neutral-500">Starting a login container…</p>
        )}
        {status.state === "awaiting_user" && (
          <div className="space-y-3 text-sm">
            <p>Open this URL and confirm the code:</p>
            <a
              className="block break-all rounded-md bg-neutral-100 p-2 font-mono text-xs underline dark:bg-neutral-800"
              href={status.url}
              target="_blank"
              rel="noopener"
            >
              {status.url}
            </a>
            <p className="text-center text-2xl font-bold tracking-widest">{status.code}</p>
            <p className="text-xs text-neutral-400">
              Waiting for approval… this dialog completes automatically.
            </p>
          </div>
        )}
        {status.state === "completed" && (
          <p className="text-sm text-green-700 dark:text-green-400">
            ✅ Credential stored. This Dev Type is ready.
          </p>
        )}
        {status.state === "failed" && (
          <p className="text-sm text-red-600">Login failed: {status.error}</p>
        )}
        <div className="mt-5 flex justify-end">
          <Button kind="ghost" onClick={onClose}>
            {status.state === "completed" ? "Done" : "Close"}
          </Button>
        </div>
      </div>
    </div>
  );
}

// ── Dev Type editor card ─────────────────────────────────────────────────────

// Per-required-file upload: the stored filename is FORCED to the registry's
// secret_file so the checklist and dispatch's _credential_spec always line up,
// whatever the local file is called. Stores immediately (server-side write).
function UploadButton({ devType, secretFile, onDone }) {
  const [msg, setMsg] = useState("");
  const inputId = `up-${devType}-${secretFile}`;
  const upload = async (file) => {
    if (!file) return;
    try {
      await send("POST", `/dev-types/${devType}/credentials`, {
        filename: secretFile,
        content: await file.text(),
      });
      setMsg("✓ stored");
      onDone && onDone();
    } catch (e) {
      setMsg(`✗ ${String(e.message || e)}`);
    }
    setTimeout(() => setMsg(""), 4000);
  };
  return (
    <span className="inline-flex items-center gap-1.5">
      <input id={inputId} type="file" className="hidden"
        onChange={(e) => { upload(e.target.files[0]); e.target.value = ""; }} />
      <label htmlFor={inputId}
        title="Stores the credential immediately — does not wait for Save"
        className="inline-flex cursor-pointer items-center gap-1 rounded border border-neutral-300 px-1.5 py-0.5 text-[11px] hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800">
        <Upload size={10} aria-hidden /> upload…
      </label>
      {msg && <span className="text-[11px] text-green-700 dark:text-green-400">{msg}</span>}
    </span>
  );
}

// Fully controlled: the card renders and edits the shared draft. Save is the
// page-level Save; only Delete / OAuth / upload act immediately.
function DevTypeCard({ name, draftDt, serverDt, harnesses, setField, onDelete, onOAuth, onCredChange }) {
  const d = draftDt;
  const set = (k, v) => setField(`devTypes.${name}.${k}`, v);
  const h = harnesses[d.harness_template] || {};   // registry info for the DRAFTED harness
  const pending = d.harness_template !== serverDt.harness_template;   // unsaved switch
  const [envSet, setEnvSet] = useState({});
  useEffect(() => {
    setEnvSet({});
    const names = (h.credential_env || []).join(",");
    if (names)
      get(`/secrets-check?harness=${encodeURIComponent(names)}`)
        .then((r) => setEnvSet(Object.fromEntries(
          Object.entries(r.harness || {}).map(([k, v]) => [k, v.present]))))
        .catch(() => {});
  }, [d.harness_template]);
  const filePresent = (sf) => (serverDt.secrets_present || []).includes(sf);
  const ready = Object.values(envSet).some(Boolean) ||
                (h.credential_files || []).some((cf) => filePresent(cf.secret_file));
  return (
    <div className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-mono text-sm font-semibold">{name}</span>
        <div className="flex items-center gap-2">
          {harnesses[serverDt.harness_template]?.oauth_available && (
            <Button kind="ghost" icon={KeyRound} onClick={() => onOAuth(name)}>
              Connect via OAuth…
            </Button>
          )}
          <Button kind="danger-ghost" icon={Trash2} onClick={() => onDelete(name)}>Delete</Button>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Harness template"
          help="Which coding agent this Dev runs: claude-code (Claude Code), grok-build (Grok Build) or codex (Codex). Authoritative — the Docker image and credential requirements below follow it automatically on Save.">
          <Select
            value={d.harness_template}
            onChange={(e) => set("harness_template", e.target.value)}
          >
            {Object.keys(harnesses).map((t) => <option key={t}>{t}</option>)}
          </Select>
        </Field>
        <Field label="Max concurrency"
          help="How many Devs of this type may run at once. The global ceiling under Limits still applies on top.">
          <Input
            type="number" min="1" value={d.max_concurrency}
            onChange={(e) => set("max_concurrency", Number(e.target.value))}
            onBlur={(e) => set("max_concurrency", Math.max(1, Number(e.target.value) || 1))}
          />
        </Field>
        <Field label="Model" hint="Empty = harness default"
          help="Pins the model the harness runs (claude --model / codex -m / grok --model), e.g. claude-fable-5 for Claude Code. Leave empty to let the harness pick its own default.">
          <Input value={d.model || ""} placeholder="e.g. claude-fable-5"
            onChange={(e) => set("model", e.target.value)} />
        </Field>
      </div>
      <Field label="Identifying prompt" hint="Delivered at the start of every run, before the mission playbook.">
        <Textarea
          rows={3} value={d.identifying_prompt}
          onChange={(e) => set("identifying_prompt", e.target.value)}
        />
      </Field>
      <Field
        label="MCP setup commands (one per line)"
        hint="⚠ Run inside the Dev container before the agent starts — arbitrary code execution by design. A failing command fails the run."
      >
        <Textarea
          rows={2}
          value={(d.mcp_setup_commands || []).join("\n")}
          onChange={(e) =>
            set("mcp_setup_commands", e.target.value.split("\n").filter(Boolean))}
        />
      </Field>
      <div className="space-y-2 rounded-md bg-stone-50 p-3 text-xs dark:bg-neutral-800/50">
        <div className="flex items-center justify-between">
          <span>
            Image <span className="font-mono">{h.docker_image || "?"}</span>
            <Help text="Derived from the harness template — not editable. Changing the harness changes the image and the credential requirements below." />
          </span>
          <span className={ready
            ? "font-medium text-green-700 dark:text-green-400"
            : "font-medium text-amber-600 dark:text-amber-400"}>
            {ready ? "✓ credentials ready" : "✗ no credentials configured"}
          </span>
        </div>
        {pending && (
          <p className="text-amber-600 dark:text-amber-400">
            ⚠ Unsaved harness change: on Save this Dev Type runs {h.docker_image} and
            needs the credentials listed below. Files under /data/secrets/{name}/
            for the old harness are kept but unused.
          </p>
        )}
        <div className="space-y-2">
          {(h.credential_env || []).map((v) => (
            <SecretField key={v} label={v}
              help={`API key for the ${d.harness_template} harness. Stored securely — never echoed, never in .env.`}
              refKey={v} checkKind="harness" paste />
          ))}
        </div>
        <ul className="space-y-1">
          {(h.credential_files || []).map((cf) => (
            <li key={cf.secret_file} className="flex items-center gap-2">
              <span>
                {filePresent(cf.secret_file) ? "✓" : "✗"} file{" "}
                <span className="font-mono">{cf.secret_file}</span>
                <span className="text-neutral-400"> → {cf.path_hint}</span>
              </span>
              <UploadButton devType={name} secretFile={cf.secret_file} onDone={onCredChange} />
            </li>
          ))}
        </ul>
        <p className="text-neutral-400">
          Any one ✓ is enough — env keys pass through at dispatch; files are delivered
          per-run over the runspec channel (stored 0600 under /data/secrets/{name}/).
        </p>
      </div>
    </div>
  );
}

// ── the page ─────────────────────────────────────────────────────────────────

// Read-only internal-forge repos (M11, founder decision: retain-by-default +
// a manual Clear). Hidden entirely when the internal forge is disabled.
function InternalReposSection({ onClear }) {
  const [data, setData] = useState(null);
  const load = () => get("/internal-repos").then(setData).catch(() => setData({ repos: [] }));
  useEffect(() => { load(); }, []);
  if (!data || data.repos.length === 0) return null;
  return (
    <Section id="internal-forge" title="Internal forge"
      description="Repositories DevCake auto-created for missions with no configured repo. Retained until you clear them; the deliverable is already in the PMO feed.">
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
      {data.ui_url && (
        <a className="mt-2 inline-block text-sm text-blue-600 hover:underline"
          href={data.ui_url} target="_blank" rel="noreferrer">Open the Gitea UI →</a>
      )}
    </Section>
  );
}

export default function ConfigPage({ section, onSectionInView, registerNavGuard }) {
  const dr = useConfigDraft();
  const [registry, setRegistry] = useState(getRegistry());
  useEffect(() => { loadRegistry().then(setRegistry); }, []);
  const [harnesses, setHarnesses] = useState({});
  const [healthInfo, setHealthInfo] = useState(null);
  const [confirm, setConfirm] = useState(null); // flip-time danger + delete confirms
  const [oauthFor, setOauthFor] = useState(null);
  const [testResult, setTestResult] = useState({});
  const [mapperMsg, setMapperMsg] = useState("");
  const [loadErr, setLoadErr] = useState("");
  const [review, setReview] = useState(null);        // {fromNav?: resolveFn}
  const [saveBusy, setSaveBusy] = useState(false);
  const [saveResults, setSaveResults] = useState(null);
  const [savedFlash, setSavedFlash] = useState(false);
  const [navPrompt, setNavPrompt] = useState(null);  // resolveFn while dialog open

  const firstLoad = useRef(true);
  const reload = async () => {
    const [c, d, a, hs, h] = await Promise.all([
      get("/config"), get("/dev-types"), get("/assignments"),
      get("/harnesses"), get("/health").catch(() => null),
    ]);
    setHarnesses(hs);
    setHealthInfo(h);
    if (firstLoad.current) { dr.load(c, d, a); firstLoad.current = false; }
    else dr.rebase(c, d, a);
  };
  useEffect(() => { reload().catch((e) => setLoadErr(String(e))); }, []);

  const loaded = dr.loaded;

  // deep-link scroll: #/config/<section> — instant on first render, smooth
  // after. Section clicks are authoritative for the sidebar highlight during
  // the programmatic scroll (quiet period), then the scrollspy resumes.
  const scrolledOnce = useRef(false);
  const spyQuietUntil = useRef(0);
  useEffect(() => {
    if (!loaded || !section) return;
    spyQuietUntil.current = Date.now() + 1000;
    onSectionInView && onSectionInView(section);
    document.getElementById(section)?.scrollIntoView({
      behavior: scrolledOnce.current ? "smooth" : "auto",
    });
    scrolledOnce.current = true;
  }, [loaded, section]);

  // scrollspy → sidebar sub-nav highlight. Scroll-position math instead of an
  // IntersectionObserver: active = last section whose top crossed the
  // activation line, clamped to the last section at the bottom of the scroll
  // (bottom sections can never reach the top of the viewport otherwise).
  useEffect(() => {
    if (!loaded || !onSectionInView) return;
    const main = document.querySelector("main");
    if (!main) return;
    const onScroll = () => {
      if (Date.now() < spyQuietUntil.current) return;
      const atBottom = main.scrollHeight - main.scrollTop - main.clientHeight < 8;
      if (atBottom) {
        onSectionInView(CONFIG_SECTIONS[CONFIG_SECTIONS.length - 1].id);
        return;
      }
      const line = main.getBoundingClientRect().top + 96; // activation line
      let active = CONFIG_SECTIONS[0].id;
      for (const s of CONFIG_SECTIONS) {
        const el = document.getElementById(s.id);
        if (el && el.getBoundingClientRect().top <= line) active = s.id;
      }
      onSectionInView(active);
    };
    main.addEventListener("scroll", onScroll, { passive: true });
    return () => main.removeEventListener("scroll", onScroll);
  }, [loaded]);

  // nav guard: register while dirty; the promise resolves from NavGuardDialog
  useEffect(() => {
    if (!registerNavGuard) return;
    registerNavGuard(
      dr.dirty
        ? () => new Promise((resolve) => setNavPrompt(() => resolve))
        : null
    );
    return () => registerNavGuard(null);
  }, [dr.dirty]);

  // browser refresh / tab close while dirty
  useEffect(() => {
    if (!dr.dirty) return;
    const h = (e) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [dr.dirty]);

  if (!loaded) return <p className="text-sm text-neutral-400">Loading…{loadErr}</p>;

  const cfg = dr.draft.cfg;
  const setField = dr.setField;

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
  const testForge = async (name) =>
    setTestResult({ ...testResult,
                    [`forge:${name}`]: await send("POST", `/connections/forge/${name}/test`) });

  const runMapper = async () => {
    setMapperMsg("Starting…");
    try {
      const r = await send("POST", "/relations-mapper/run");
      setMapperMsg(`✓ dispatched ${r.run_id} — watch it on the Runs page`);
    } catch (e) {
      setMapperMsg(`✗ ${String(e.message || e)}`);
    }
  };

  const rm = cfg.relations_mapper || { enabled: false, interval_minutes: 60, dev_type: null };
  const serverRm = dr.server.cfg.relations_mapper || {};
  const mapperDirty = dr.diff.some((x) => x.path.startsWith("cfg.relations_mapper"));

  // harness-mismatch advisory for an assignment row (replaces the old
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

  const doSave = async () => {
    setSaveBusy(true);
    const diff = dr.diff;
    const results = {};
    const cfgKeys = [...new Set(diff.filter((x) => x.path.startsWith("cfg."))
      .map((x) => x.path.split(".")[1]))];
    if (cfgKeys.length) {
      const patch = Object.fromEntries(cfgKeys.map((k) => [k, dr.draft.cfg[k]]));
      try { await send("PUT", "/config", patch); results["config"] = { ok: true }; }
      catch (e) { results["config"] = { ok: false, error: String(e.message || e) }; }
    }
    const dtNames = [...new Set(diff.filter((x) => x.path.startsWith("devTypes."))
      .map((x) => x.path.split(".")[1]))];
    for (const nm of dtNames) {
      try {
        await send("PUT", `/dev-types/${nm}`, dr.draft.devTypes[nm]);
        results[`dev type ${nm}`] = { ok: true };
      } catch (e) {
        results[`dev type ${nm}`] = { ok: false, error: String(e.message || e) };
      }
    }
    if (diff.some((x) => x.path.startsWith("assignments."))) {
      try { await send("PUT", "/assignments", dr.draft.assignments); results["assignments"] = { ok: true }; }
      catch (e) { results["assignments"] = { ok: false, error: String(e.message || e) }; }
    }
    // refetch + rebase: saved edits match the fresh server and vanish from the
    // diff; failed units keep their drafted leaves → the page stays dirty with
    // exactly the failed subset
    try { await reload(); } catch { /* page keeps last state */ }
    setSaveBusy(false);
    const ok = Object.values(results).every((r) => r.ok);
    if (ok) {
      setReview(null);
      setSaveResults(null);
      setSavedFlash(true);
      setTimeout(() => setSavedFlash(false), 2500);
    } else {
      setSaveResults(results);
    }
    return ok;
  };

  const confirmSave = async () => {
    const fromNav = review?.fromNav;
    const ok = await doSave();
    if (fromNav) fromNav(ok); // proceed only on full success
  };

  const closeReview = () => {
    if (review?.fromNav) review.fromNav(false);
    setReview(null);
    setSaveResults(null);
  };

  const rows = describeDiff(dr.diff);

  return (
    <div className="space-y-5">
      <PageHeader title="Configuration"
        subtitle="Connections, Dev Types, assignments and limits — edits apply on Save" />

      {/* mobile section chips (sidebar sub-nav is expanded-drawer-only) */}
      <div className="sticky top-0 z-20 -mx-4 flex gap-1.5 overflow-x-auto bg-surface/90 px-4 py-2 backdrop-blur dark:bg-surface-dark/90 lg:hidden">
        {CONFIG_SECTIONS.map((s) => (
          <a key={s.id} href={`#/config/${s.id}`}
            onClick={() => document.getElementById(s.id)?.scrollIntoView({ behavior: "smooth" })}
            className="shrink-0 rounded-full border border-neutral-200 bg-surface-raised px-3 py-1 text-xs font-medium text-neutral-600 dark:border-neutral-800 dark:bg-surface-raised-dark dark:text-neutral-300">
            {s.label}
          </a>
        ))}
      </div>

      <Section id="traffic" title="Traffic control"
        description="The Relations Mapper. (Mission intake is the master switch in the sidebar — it applies immediately.)">
        <div className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-sm font-semibold">
              Relations Mapper
              <Help text="A Dev tasked strictly with mapping missing 'blocked by' relations between existing missions (it reads every open mission's title and description head). Proposed relations are validated by the app and appear in your PMO; delete a relation there to undo it." />
            </span>
            <div className="flex items-center gap-2">
              <ImmediateBadge text="runs with saved settings" />
              <Button kind="ghost" icon={Play} onClick={runMapper}
                disabled={!serverRm.dev_type || mapperDirty}
                title={mapperDirty
                  ? "Save your mapper changes first — Run now uses the saved settings"
                  : !serverRm.dev_type ? "Pick and save a Dev Type first" : undefined}>
                Run now
              </Button>
            </div>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field label="Dev Type"
              help="Which Dev Type runs the mapper. The seeded junior-dev (a cheap, fast model) is the default — ordering judgment from titles and description heads doesn't need a heavyweight.">
              <Select value={rm.dev_type || ""}
                onChange={(e) => {
                  setField("cfg.relations_mapper.dev_type", e.target.value || null);
                  if (!e.target.value) setField("cfg.relations_mapper.enabled", false);
                }}>
                <option value="">(none)</option>
                {dr.order.map((n) => <option key={n} value={n}>{n}</option>)}
              </Select>
            </Field>
            <Field label="Interval (minutes)"
              help="Cadence of the periodic service when enabled. The first automatic run happens one interval after the app starts; use Run now for an immediate pass.">
              <Input type="number" min="1" value={rm.interval_minutes}
                onChange={(e) => setField("cfg.relations_mapper.interval_minutes", Number(e.target.value))}
                onBlur={(e) => setField("cfg.relations_mapper.interval_minutes",
                  Math.max(1, Number(e.target.value) || 60))} />
            </Field>
            <Field label="Periodic service" hint="Default OFF — use Run now for one-shot passes">
              <div className="flex h-9 items-center gap-3 text-sm">
                <Toggle on={rm.enabled} label="Periodic service"
                  onClick={() => {
                    if (!rm.enabled && !rm.dev_type) {
                      setMapperMsg("✗ pick a Dev Type first");
                      return;
                    }
                    setField("cfg.relations_mapper.enabled", !rm.enabled);
                  }} />
                <span>{rm.enabled ? "ON — runs on the interval" : "OFF — manual only"}</span>
              </div>
            </Field>
          </div>
          {healthInfo?.mapper_degraded && (
            <p className="text-sm text-amber-600 dark:text-amber-400">
              ⚠ Periodic service backing off — the last 3 mapper runs failed
              ({healthInfo.mapper_degraded}). Run now still works; a successful run resumes
              the schedule.
            </p>
          )}
          {mapperMsg && (
            <p className={`text-sm ${mapperMsg.startsWith("✗") ? "text-red-600" : "text-green-700 dark:text-green-400"}`}>
              {mapperMsg}
            </p>
          )}
        </div>
      </Section>

      <Section id="pmo" title="PMO connections"
        description={`The PMO teams DevCake watches (one instance each), and how it adopts missions. Supported: ${registry.pmo_systems.map((s) => s.display_name).join(", ")}. Instance names prefix branches and run ids (LINEAR-DEV-17).`}>
        {cfg.pmos.map((inst, idx) => {
          const tr = testResult[`pmo:${inst.name}`];
          return (
            <div key={idx} className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-mono text-sm font-semibold">{inst.name || "(unnamed)"}</span>
                {cfg.pmos.length > 1 && (
                  <Button kind="danger-ghost" onClick={() =>
                    setField("cfg.pmos", cfg.pmos.filter((_, i) => i !== idx))}>
                    Remove
                  </Button>
                )}
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <Field label="Instance name"
                  help="Operator-chosen identity (lowercase letters/digits, ≤12, no hyphens). Uppercased, it prefixes this instance's branches and run ids — choose once, renaming strands in-flight missions.">
                  <Input value={inst.name}
                  onChange={(e) => setField(`cfg.pmos.${idx}.name`, e.target.value)} /></Field>
                <Field label="Team key"
                  help="The team's short key — the prefix of its issue IDs (PRJ for PRJ-123). This instance watches only this team. Empty = instance stays idle.">
                  <Input value={inst.team_key}
                  onChange={(e) => setField(`cfg.pmos.${idx}.team_key`, e.target.value)} /></Field>
                <SecretField label="API key"
                  help="This instance's PMO API key. Stored securely on the app volume — never echoed back, never in .env."
                  refKey={`pmo:${inst.name}:api_key`} paste />
                <Field label="Default repo"
                  help="Where this instance's missions land when they carry no `devcake-repo:` marker. '(none)' = unrouted missions wait (the internal fallback forge arrives in v0.1 M11).">
                  <Select value={inst.default_repo || ""}
                    onChange={(e) => setField(`cfg.pmos.${idx}.default_repo`, e.target.value || null)}>
                    <option value="">(none)</option>
                    {cfg.repos.map((r) => (
                      <option key={r.name} value={r.name}>{r.name}</option>
                    ))}
                  </Select>
                </Field>
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
        <div className="flex flex-wrap items-center gap-3">
          <Button kind="ghost" onClick={() =>
            setField("cfg.pmos", [...cfg.pmos,
              { name: `linear${cfg.pmos.length + 1}`, system: "linear",
                team_key: "", api_base: null, default_repo: null }])}>
            + Add PMO instance
          </Button>
          <Field label="Poll interval (s)"
            help="How often DevCake polls each PMO instance for new or changed missions. Lower = faster pickup, more API calls.">
            <Input type="number"
            value={cfg.poll_interval_seconds}
            onChange={(e) => setField("cfg.poll_interval_seconds", Number(e.target.value))} /></Field>
        </div>
        <Field label="Adoption mode"
          help="opt-in: DevCake only adopts items you label DEVCAKE. opt-out: it adopts every non-completed issue and project in the team, including the backlog.">
          <div className="flex items-center gap-3 text-sm">
            <span className={cfg.adoption_mode === "opt_in" ? "font-semibold" : "text-neutral-400"}>
              opt-in (label required)
            </span>
            <Toggle on={cfg.adoption_mode === "opt_out"} label="Adoption mode"
              onClick={() =>
                cfg.adoption_mode === "opt_in"
                  ? guardedFlip("cfg.adoption_mode", "opt_out", "Adopt the ENTIRE team?",
                      ADOPTION_COPY + "\n\n(Drafted now; applies when you Save.)")
                  : setField("cfg.adoption_mode", "opt_in")} />
            <span className={cfg.adoption_mode === "opt_out" ? "font-semibold" : "text-neutral-400"}>
              opt-out (whole team)
            </span>
          </div>
        </Field>
      </Section>

      <Section id="repository" title="Repositories"
        description="Forge connections, access tokens and merge policy. Missions route to a repo via a `devcake-repo:<name>` line in their description, else the PMO instance's default repo; unrouted missions wait.">
        {cfg.repos.map((repo, idx) => {
          const tr = testResult[`forge:${repo.name}`];
          return (
            <div key={idx} className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-mono text-sm font-semibold">{repo.name || "(unnamed)"}</span>
                {cfg.repos.length > 0 && (
                  <Button kind="danger-ghost" onClick={() =>
                    setField("cfg.repos", cfg.repos.filter((_, i) => i !== idx))}>
                    Remove
                  </Button>
                )}
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <Field label="Repo name"
                  help="Operator-chosen identity (lowercase letters/digits, ≤12, no hyphens). Missions reference it in `devcake-repo:` markers and PMO default-repo settings.">
                  <Input value={repo.name}
                  onChange={(e) => setField(`cfg.repos.${idx}.name`, e.target.value)} /></Field>
                <Field label="Forge"
                  help="Where the repository lives. Selects the API DevCake uses for pull/merge requests, approvals and merges.">
                  <Select value={repo.forge}
                    onChange={(e) => setField(`cfg.repos.${idx}.forge`, e.target.value)}>
                    {registry.forges.map((f) => (
                      <option key={f.id} value={f.id}>{f.id}</option>
                    ))}
                  </Select>
                </Field>
                <Field label="Repository URL"
                  help="HTTPS URL of the repository, e.g. https://github.com/you/repo.git. Devs clone it; the app opens and merges PRs on it. Empty = repo stays idle.">
                  <Input value={repo.url}
                  onChange={(e) => setField(`cfg.repos.${idx}.url`, e.target.value)} /></Field>
                <SecretField label="Access token"
                  help="This repo's forge token (repo read/write + PR scopes). Stored securely — never echoed, never in .env."
                  refKey={`repo:${repo.name}:token`} paste />
                <SecretField label="Read-only token" hint="Optional → clone-only for PLAN/REVIEW/ONBOARD"
                  help="Optional read-only token used by non-EXECUTE stages so a prompt-injected Dev can't push. Leave empty to give every stage the write token."
                  refKey={`repo:${repo.name}:token_ro`} paste />
                <SecretField label="Reviewer token" hint="Optional 2nd account → formal PR approvals"
                  help="Optional second account's token. When set, REVIEW posts a formal approval from that account before merging."
                  refKey={`repo:${repo.name}:reviewer_token`} paste />
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <Button kind="ghost" onClick={() => testForge(repo.name)}>Test connection</Button>
                <ImmediateBadge text="tests saved values" />
                {tr && (
                  <span className={`text-sm ${tr.ok ? "text-green-700 dark:text-green-400" : "text-red-600"}`}>
                    {tr.ok
                      ? `✓ ${tr.forge} reachable · reviewer token: ${tr.reviewer_token_configured ? "yes" : "no"}`
                      : `✗ ${tr.error}`}
                  </span>
                )}
              </div>
            </div>
          );
        })}
        <Button kind="ghost" onClick={() =>
          setField("cfg.repos", [...cfg.repos,
            { name: `repo${cfg.repos.length + 1}`, forge: "github", url: "",
              api_base: null, default_branch: "main" }])}>
          + Add repository
        </Button>
        <Field label="Auto-merge"
          help="ON: after DevCake's REVIEW step approves a PR, it merges itself (squash). OFF: DevCake stops at DEVCAKE-MERGE and waits for you to merge.">
          <div className="flex items-center gap-3 text-sm">
            <Toggle on={cfg.auto_merge} label="Auto-merge"
              onClick={() =>
                cfg.auto_merge
                  ? setField("cfg.auto_merge", false)
                  : guardedFlip("cfg.auto_merge", true, "Merge without human review?",
                      AUTO_MERGE_COPY + "\n\n(Drafted now; applies when you Save.)")} />
            <span>{cfg.auto_merge ? "ON — approved PRs merge themselves" : "OFF — every merge is yours (DEVCAKE-MERGE handoff)"}</span>
          </div>
        </Field>
        {/* dependent controls: only meaningful while auto-merge is ON (drafted value) */}
        <div className={cfg.auto_merge ? "" : "opacity-50 pointer-events-none"}
          aria-disabled={!cfg.auto_merge}>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Auto-resolve merge conflicts"
              help="Only applies when auto-merge is ON. When a merge fails on conflicts, DevCake sends the mission back to EXECUTE to sync the branch and resolve them (max 2 attempts) instead of waiting for you at DEVCAKE-MERGE.">
              <div className="flex items-center gap-3 text-sm">
                <Toggle on={cfg.auto_resolve_merge_conflicts} label="Auto-resolve merge conflicts"
                  disabled={!cfg.auto_merge}
                  onClick={() => cfg.auto_merge &&
                    setField("cfg.auto_resolve_merge_conflicts", !cfg.auto_resolve_merge_conflicts)} />
                <span>{cfg.auto_resolve_merge_conflicts ? "ON — conflicts go back to EXECUTE (max 2 tries)" : "OFF — conflicts wait for you (DEVCAKE-MERGE)"}</span>
              </div>
            </Field>
            <Field label="Merge retry window (min)"
              help="When a merge isn't possible yet (CI running, mergeability computing), DevCake keeps retrying via the merge sweep for this long before handing off with DEVCAKE-MERGE. Lower it on CI-light repos to surface unmergeable PRs faster; raise it on CI-heavy repos. 0 = hand off immediately.">
              <Input type="number" min="0"
                disabled={!cfg.auto_merge}
                value={cfg.merge_retry_window_minutes}
                onChange={(e) => setField("cfg.merge_retry_window_minutes",
                  Math.max(0, Number(e.target.value)))} />
            </Field>
          </div>
        </div>
      </Section>

      <InternalReposSection
        onClear={(name) => setConfirm({
          title: `Clear internal repo '${name}'?`,
          body: "Deletes the auto-created repository, its machine user (revoking "
            + "its tokens), and the stored credentials. The deliverable already "
            + "attached to the PMO feed is untouched. This cannot be undone.",
          confirmLabel: "Delete the internal repo",
          danger: true,
          action: async () => {
            setConfirm(null);
            try { await send("DELETE", `/internal-repos/${name}`); }
            catch (e) { setLoadErr(String(e.message || e)); }
          },
        })} />

      <Section id="dev-types" title="Dev Types"
        description="Agent configurations — harness, model, concurrency and credentials."
        actions={
          <>
            <ImmediateBadge text="create/delete apply immediately" />
            <Button kind="ghost" icon={Plus}
              onClick={async () => {
                const name = `new-dev-${Date.now() % 1000}`;
                await send("POST", "/dev-types", {
                  name, harness_template: "codex", identifying_prompt: "",
                  max_concurrency: 1,
                });
                reload();
              }}>
              New Dev Type
            </Button>
          </>
        }>
        <div className="grid gap-4 lg:grid-cols-2">
          {dr.order.filter((n) => dr.draft.devTypes[n] && dr.server.devTypes[n]).map((name) => (
            <DevTypeCard key={name} name={name}
              draftDt={dr.draft.devTypes[name]}
              serverDt={dr.server.devTypes[name]}
              harnesses={harnesses}
              setField={setField}
              onCredChange={reload}
              onDelete={(nm) => {
                const hasEdits = dr.diff.some((x) => x.path.startsWith(`devTypes.${nm}.`));
                setConfirm({
                  title: `Delete dev type ${nm}?`,
                  body: "Its config file is removed; credentials under /data/secrets stay until cleaned manually."
                    + (hasEdits ? "\n\nThis card has unsaved edits — deleting discards them." : ""),
                  confirmLabel: "Delete",
                  action: async () => {
                    try { await send("DELETE", `/dev-types/${nm}`); await reload(); }
                    catch (e) { setLoadErr(String(e)); }
                    setConfirm(null);
                  },
                });
              }}
              onOAuth={setOauthFor} />
          ))}
        </div>
      </Section>

      <Section id="assignments" title="Assignments"
        description="Which Dev Type handles each mission type.">
        {dr.draft.assignments?.EXECUTE?.dev_type
          && dr.draft.assignments?.REVIEW?.dev_type
          && dr.draft.assignments.EXECUTE.dev_type === dr.draft.assignments.REVIEW.dev_type && (
          <div className="mb-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-200">
            EXECUTE and REVIEW share the same Dev Type. Independent AI review is the
            default configuration, not a hard invariant — consider assigning different
            types (and models) for review independence.
          </div>
        )}
        <div className="overflow-x-auto">
          <table className="w-full min-w-[32rem] text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-neutral-400">
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
      </Section>

      <Section id="limits" title="Limits"
        description="Global concurrency and safety ceilings. Dev container Docker HostConfig CPU/memory is not available in Dagu 2.10.5 — concurrency caps are the real throttle.">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label="Global max Devs" hint="Effective ceiling = min(global, Σ per-type caps). Primary host-protection control.">
            <Input type="number" value={cfg.concurrency.global_max}
              onChange={(e) => setField("cfg.concurrency.global_max", Number(e.target.value))} />
          </Field>
          <Field label="Dev run timeout (min)"
            help="Wall-clock limit per Dev run (dispatched/running only — finalizing is never timeout-killed). The mission is retried up to its attempt limit.">
            <Input type="number" value={cfg.dev_timeout_minutes}
              onChange={(e) => setField("cfg.dev_timeout_minutes", Number(e.target.value))} />
          </Field>
          <Field label="Loop warning every N rejections"
            help="When REVIEW keeps rejecting EXECUTE's work, DevCake posts a warning to the mission's activity feed every N rejections so you can intervene. Must be ≥ 1.">
            <Input type="number" min={1} value={cfg.review_loop_warning_every}
              onChange={(e) => setField("cfg.review_loop_warning_every", Number(e.target.value))} />
          </Field>
        </div>
        <div className="mt-4 rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2 text-sm text-neutral-600 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-300">
          <strong className="font-medium text-neutral-800 dark:text-neutral-100">Dev container limits:</strong>{" "}
          Dagu 2.10.5 cannot apply Docker HostConfig CPU/memory/PID limits to Dev
          containers; <code className="font-mono text-xs">dagu/dags/dev-run.yaml</code>{" "}
          carries a best-effort <code className="font-mono text-xs">resources.limits</code>{" "}
          block, and concurrency caps above are the real throttle (docs/07 §7,
          hard limits are v0.1 backlog).
        </div>
        <div className="mt-4 rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2 text-sm text-neutral-600 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-300">
          <strong className="font-medium text-neutral-800 dark:text-neutral-100">Compose restart:</strong>{" "}
          long-lived services use <code className="font-mono text-xs">restart: unless-stopped</code> in
          docker-compose.yml (default on). The SPA cannot rewrite compose — set{" "}
          <code className="font-mono text-xs">restart: &quot;no&quot;</code> in the file to disable.
        </div>
      </Section>

      <DirtyBar
        count={dr.diff.length}
        errors={dr.errors}
        saved={savedFlash}
        onDiscard={dr.discard}
        onSave={() => setReview({})}
      />

      <ConfirmDialog open={!!confirm} {...(confirm || {})}
        onConfirm={() => confirm.action()}
        onCancel={() => setConfirm(null)} />
      <SaveReviewDialog
        open={!!review}
        rows={rows}
        busy={saveBusy}
        results={saveResults}
        onConfirm={confirmSave}
        onCancel={closeReview}
        onRetry={doSave}
      />
      <NavGuardDialog
        open={!!navPrompt}
        count={dr.diff.length}
        onStay={() => { navPrompt(false); setNavPrompt(null); }}
        onDiscard={() => { dr.discard(); navPrompt(true); setNavPrompt(null); }}
        onSave={() => { setReview({ fromNav: navPrompt }); setNavPrompt(null); }}
      />
      {oauthFor && <OAuthWizard devType={oauthFor}
        onClose={() => { setOauthFor(null); reload(); }} />}
    </div>
  );
}
