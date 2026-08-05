import React from "react";
import { ExternalLink, Route, Container, Search, Workflow, ListChecks,
         GitBranch, BookOpen, FolderGit2 } from "lucide-react";
import PageHeader from "../components/PageHeader.jsx";
import { Card } from "../components/Card.jsx";

// The external consoles DevCake runs on (renamed from Logs, 2026-08-02):
// one card per console — what lives there, how to find your run, one
// primary "Open" per card. Gitea appears only when the internal forge is
// enabled (its URL rides /health, not window.DEVCAKE).
const cfg = window.DEVCAKE || {};

function ConsoleCard({ title, blurb, pointers, href, cta }) {
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-base font-semibold tracking-tight">{title}</h3>
          <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-300">{blurb}</p>
        </div>
        <a
          href={href}
          target="_blank"
          rel="noopener"
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white transition hover:bg-accent-700"
        >
          {cta} <ExternalLink size={13} aria-hidden />
        </a>
      </div>
      <ul className="mt-4 space-y-2.5">
        {pointers.map(({ icon: Icon, text }, i) => (
          <li key={i} className="flex items-start gap-2.5 text-sm text-neutral-600 dark:text-neutral-300">
            <Icon size={15} className="mt-0.5 shrink-0 text-accent-600 dark:text-accent-400" aria-hidden />
            <span>{text}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

export default function ConsolesPage({ health = {} }) {
  const oo = cfg.ooUrl || "http://localhost:5080";
  const dagu = cfg.daguUrl || "http://localhost:8525";
  const gitea = health.internal_forge;

  return (
    <div className="space-y-4">
      <PageHeader title="Consoles"
        subtitle="The external UIs DevCake runs on — traces, execution history, and the internal forge" />

      <ConsoleCard
        title="OpenObserve — traces, logs and token costs"
        href={oo}
        cta="Open OpenObserve"
        blurb={<>Every Dev run is one trace (dispatch → container → finalization). Token counts and cost
          ride each <code>dev.run</code> span as <code>devcake.*</code> attributes; each step also
          posts a human-readable token report to the mission's activity feed.</>}
        pointers={[
          { icon: Route, text: <>Traces live in the <code>default</code> trace stream.</> },
          { icon: Container, text: <>Container stdout (dagu, redis) lands in the <code>container_logs</code> log stream.</> },
          { icon: Search, text: <>Search a run by its id, e.g. <code>DEV-17-3-EXECUTE-560E6T</code>.</> },
        ]}
      />

      <ConsoleCard
        title="Dagu — the run executor"
        href={dagu}
        cta="Open Dagu"
        blurb={<>Every Dev container is launched as a Dagu DAG named after its run id. Dagu holds the
          executor's own view: step-level status, timing, and the captured step log — the same
          condensed output the run terminal streams.</>}
        pointers={[
          { icon: Workflow, text: <>One DAG per run — the DAG name is the run id.</> },
          { icon: ListChecks, text: <>Step history shows dispatch, container execution, and exit codes as Dagu saw them.</> },
          { icon: Search, text: <>The Runs page deep-links here via its Open Dagu button; a stopped run shows as canceled.</> },
        ]}
      />

      {gitea && (
        <ConsoleCard
          title="Gitea — the internal forge"
          href={gitea.ui_url || "http://localhost:3300"}
          cta="Open Gitea"
          blurb={<>The bundled forge behind zero-repo missions, operator-created internal repositories,
            per-mission activity repos, and the skill store. Sign in with the admin account from
            your stack's <code>.env</code>.</>}
          pointers={[
            { icon: GitBranch, text: <>Mission repos live in <code>devcake-internal</code>; operator repos in <code>devcake-repos</code>.</> },
            { icon: BookOpen, text: <>The skill store is a repo here — skills are readable (and human-editable) as files.</> },
            { icon: FolderGit2, text: <>The Repositories page lists these same internal repos with a Clear action.</> },
          ]}
        />
      )}
    </div>
  );
}
