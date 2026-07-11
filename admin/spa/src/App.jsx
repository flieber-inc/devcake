import React, { useEffect, useState } from "react";
import ConfigTab from "./tabs/ConfigTab.jsx";
import ExecutorTab from "./tabs/ExecutorTab.jsx";
import LogsTab from "./tabs/LogsTab.jsx";
import { get } from "./api.js";

const TABS = { Config: ConfigTab, Executor: ExecutorTab, Logs: LogsTab };

function Dot({ ok, label }) {
  const color = ok === true ? "bg-green-600" : ok === false ? "bg-red-600" : "bg-neutral-400";
  return (
    <span className="flex items-center gap-1.5 text-xs text-neutral-500 dark:text-neutral-400">
      <span className={`inline-block h-2 w-2 rounded-full ${color}`} />
      {label}
    </span>
  );
}

export default function App() {
  const [tab, setTab] = useState("Config");
  const [health, setHealth] = useState({});
  useEffect(() => {
    const load = () => get("/health").then(setHealth).catch(() => setHealth({}));
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);
  const Body = TABS[tab];
  const breakers = Object.entries(health.circuit_breakers || {});
  return (
    <div className="min-h-screen bg-stone-100 text-neutral-900 dark:bg-neutral-950 dark:text-neutral-100">
      <header className="flex items-center justify-between border-b border-neutral-200 bg-white px-6 py-3 dark:border-neutral-800 dark:bg-neutral-900">
        <div className="text-lg">
          🍰 <span className="font-semibold">DevCake</span>{" "}
          <span className="ml-1 text-sm text-neutral-400">agentic developer</span>
        </div>
        <div className="flex gap-4">
          <Dot ok={health.app} label="app" />
          <Dot ok={health.pmo} label="linear" />
          <Dot ok={health.redis} label="redis" />
          <Dot ok={health.dagu} label="dagu" />
          <Dot ok={health.openobserve} label="logs" />
        </div>
      </header>
      {breakers.length > 0 && (
        <div className="border-b border-amber-300 bg-amber-50 px-6 py-2 text-sm text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-200">
          ⚠️ Circuit breaker tripped: {breakers.map(([k, v]) => `${k} (${v})`).join(" · ")} —
          upload or refresh that Dev Type's credential to resume.
        </div>
      )}
      <nav className="flex gap-1 px-6 pt-4">
        {Object.keys(TABS).map((name) => (
          <button
            key={name}
            onClick={() => setTab(name)}
            className={`rounded-t-lg border border-b-0 px-4 py-2 text-sm ${
              tab === name
                ? "border-neutral-200 bg-white font-semibold dark:border-neutral-800 dark:bg-neutral-900"
                : "border-transparent text-neutral-500 hover:text-neutral-800 dark:hover:text-neutral-200"
            }`}
          >
            {name}
          </button>
        ))}
      </nav>
      <main className="px-6 pb-10">
        <div className="rounded-b-xl rounded-tr-xl border border-neutral-200 bg-white p-6 dark:border-neutral-800 dark:bg-neutral-900">
          <Body />
        </div>
      </main>
      <footer className="px-6 pb-4 text-xs text-neutral-400">
        DevCake v0 ·{" "}
        <a className="underline" href="https://github.com/fidecastro/devcake" target="_blank" rel="noopener">
          spec &amp; source
        </a>
      </footer>
    </div>
  );
}
