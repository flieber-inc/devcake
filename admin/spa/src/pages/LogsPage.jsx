import React from "react";
import { ExternalLink, Route, Container, Search } from "lucide-react";
import PageHeader from "../components/PageHeader.jsx";
import { Card } from "../components/Card.jsx";

const cfg = window.DEVCAKE || {};
const oo = cfg.ooUrl || "http://localhost:5080";

const POINTERS = [
  { icon: Route, text: <>Traces live in the <code>default</code> trace stream.</> },
  { icon: Container, text: <>Container stdout (dagu, redis) lands in the <code>container_logs</code> log stream.</> },
  { icon: Search, text: <>Search a run by its id, e.g. <code>DEV-17-3-EXECUTE-560E6T</code>.</> },
];

export default function LogsPage() {
  return (
    <div className="space-y-4">
      <PageHeader title="Logs" subtitle="Traces, logs and token costs in OpenObserve"
        actions={
          <a
            href={oo}
            target="_blank"
            rel="noopener"
            className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white transition hover:bg-accent-700"
          >
            Open OpenObserve <ExternalLink size={13} aria-hidden />
          </a>
        } />
      <Card className="p-5">
        <p className="text-sm text-neutral-600 dark:text-neutral-300">
          Every Dev run is one trace (dispatch → container → finalization). Token counts and cost
          ride each <code>dev.run</code> span as <code>devcake.*</code> attributes; each step also
          posts a human-readable token report to the mission's activity feed.
        </p>
        <ul className="mt-4 space-y-2.5">
          {POINTERS.map(({ icon: Icon, text }, i) => (
            <li key={i} className="flex items-start gap-2.5 text-sm text-neutral-600 dark:text-neutral-300">
              <Icon size={15} className="mt-0.5 shrink-0 text-accent-600 dark:text-accent-400" aria-hidden />
              <span>{text}</span>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
