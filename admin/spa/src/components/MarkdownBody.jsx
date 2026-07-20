import React from "react";
import ReactMarkdown from "react-markdown";
import { safeHref } from "../lib/markdown.js";

// Compact admin typography under Tailwind preflight — token classes only
// (DESIGN.md). Headings demoted one level so dialog chrome keeps the outline top.

const h = (Tag, cls) =>
  function MdHeading({ children }) {
    return <Tag className={cls}>{children}</Tag>;
  };

const components = {
  h1: h("h2", "mt-4 mb-2 text-base font-semibold tracking-tight text-neutral-900 first:mt-0 dark:text-neutral-100"),
  h2: h("h3", "mt-4 mb-1.5 text-sm font-semibold tracking-tight text-neutral-900 first:mt-0 dark:text-neutral-100"),
  h3: h("h4", "mt-3 mb-1 text-sm font-semibold text-neutral-800 first:mt-0 dark:text-neutral-200"),
  h4: h("h5", "mt-3 mb-1 text-sm font-semibold text-neutral-800 first:mt-0 dark:text-neutral-200"),
  h5: h("h6", "mt-2 mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-600 first:mt-0 dark:text-neutral-400"),
  h6: h("h6", "mt-2 mb-1 text-xs font-semibold text-neutral-600 first:mt-0 dark:text-neutral-400"),
  p: ({ children }) => (
    <p className="my-2 text-sm leading-relaxed text-neutral-800 first:mt-0 last:mb-0 dark:text-neutral-200">
      {children}
    </p>
  ),
  ul: ({ children }) => (
    <ul className="my-2 list-disc space-y-1 pl-5 text-sm text-neutral-800 first:mt-0 last:mb-0 dark:text-neutral-200">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="my-2 list-decimal space-y-1 pl-5 text-sm text-neutral-800 first:mt-0 last:mb-0 dark:text-neutral-200">
      {children}
    </ol>
  ),
  li: ({ children }) => (
    <li className="leading-relaxed marker:text-neutral-400 dark:marker:text-neutral-500">
      {children}
    </li>
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-neutral-300 pl-3 text-sm text-neutral-600 italic dark:border-neutral-600 dark:text-neutral-400">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-4 border-neutral-200 dark:border-neutral-700" />,
  a: ({ href, children }) => {
    const safe = safeHref(href);
    if (!safe) {
      return <span className="text-neutral-800 dark:text-neutral-200">{children}</span>;
    }
    const external = /^https?:\/\//i.test(safe);
    return (
      <a
        href={safe}
        className="font-medium text-accent-600 underline-offset-2 hover:underline dark:text-accent-400"
        {...(external
          ? { target: "_blank", rel: "noopener noreferrer" }
          : {})}>
        {children}
      </a>
    );
  },
  // v1: no remote images (egress / tracking in the admin session)
  img: () => null,
  pre: ({ children }) => (
    <pre className="my-2 overflow-x-auto rounded-md border border-neutral-200 bg-stone-50 p-2.5 font-mono text-xs leading-relaxed text-neutral-800 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-200">
      {children}
    </pre>
  ),
  code: ({ className, children, ...props }) => {
    // fenced blocks: react-markdown puts className on <code> inside <pre>
    const fenced = Boolean(className);
    if (fenced) {
      return (
        <code className={`font-mono text-xs ${className || ""}`} {...props}>
          {children}
        </code>
      );
    }
    return (
      <code
        className="rounded bg-stone-100 px-1 py-0.5 font-mono text-[0.8em] text-neutral-800 dark:bg-neutral-800 dark:text-neutral-200"
        {...props}>
        {children}
      </code>
    );
  },
  strong: ({ children }) => (
    <strong className="font-semibold text-neutral-900 dark:text-neutral-100">{children}</strong>
  ),
  em: ({ children }) => <em className="italic">{children}</em>,
  table: ({ children }) => (
    <div className="my-2 overflow-x-auto">
      <table className="w-full min-w-[16rem] border-collapse text-left text-sm">
        {children}
      </table>
    </div>
  ),
  thead: ({ children }) => (
    <thead className="text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
      {children}
    </thead>
  ),
  th: ({ children }) => (
    <th className="border-b border-neutral-200 px-2 py-1.5 font-semibold dark:border-neutral-700">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border-t border-neutral-100 px-2 py-1.5 align-top text-neutral-800 dark:border-neutral-800 dark:text-neutral-200">
      {children}
    </td>
  ),
};

/**
 * Render Markdown as compact admin prose (token styles, no raw HTML).
 * Shell chrome (scroll/border) belongs to the dialog.
 */
export default function MarkdownBody({ children, className = "" }) {
  const text = typeof children === "string" ? children : String(children ?? "");
  return (
    <div className={`markdown-body text-sm ${className}`.trim()}>
      <ReactMarkdown components={components}>{text}</ReactMarkdown>
    </div>
  );
}

/** Shared scroll shell for Rendered + Source so both modes share chrome. */
export function MarkdownViewShell({ children, className = "" }) {
  return (
    <div
      className={`max-h-[60vh] overflow-auto rounded-md border border-neutral-200 bg-stone-50 p-3 dark:border-neutral-700 dark:bg-neutral-900 ${className}`.trim()}>
      {children}
    </div>
  );
}

/** Segmented Rendered | Source control (matches Skills Write/Import chips). */
export function MarkdownModeToggle({ mode, onChange, disabled }) {
  return (
    <div
      className="flex gap-0.5 rounded-md bg-stone-100 p-0.5 text-xs dark:bg-neutral-800"
      role="group"
      aria-label="View mode">
      {[["rendered", "Rendered"], ["source", "Source"]].map(([m, label]) => (
        <button
          key={m}
          type="button"
          disabled={disabled}
          aria-pressed={mode === m}
          onClick={() => onChange(m)}
          className={`rounded px-2.5 py-1 font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 disabled:cursor-not-allowed disabled:opacity-50 ${
            mode === m
              ? "bg-white text-neutral-900 shadow-sm dark:bg-neutral-700 dark:text-neutral-100"
              : "text-neutral-500 hover:text-neutral-700 dark:hover:text-neutral-300"
          }`}>
          {label}
        </button>
      ))}
    </div>
  );
}

/** Source (raw) body inside MarkdownViewShell. */
export function MarkdownSourcePre({ children }) {
  return (
    <pre className="whitespace-pre-wrap font-mono text-xs leading-relaxed text-neutral-800 dark:text-neutral-200">
      {children}
    </pre>
  );
}
