// Pure helpers for admin Markdown View (skills / prompt templates).

/** True for paths we treat as Markdown (case-insensitive). */
export function isMarkdownPath(path) {
  if (!path || typeof path !== "string") return false;
  const lower = path.toLowerCase();
  return lower.endsWith(".md") || lower.endsWith(".markdown");
}

/**
 * Strip a leading YAML frontmatter fence only.
 * Unclosed fences and mid-body `---` are left untouched.
 */
export function stripYamlFrontmatter(text) {
  if (!text || typeof text !== "string") return text || "";
  const m = text.match(/^---\r?\n[\s\S]*?\r?\n---\r?\n/);
  return m ? text.slice(m[0].length) : text;
}

/** Allow only http(s) and mailto for rendered links. */
export function safeHref(href) {
  if (!href || typeof href !== "string") return null;
  const t = href.trim();
  if (/^https?:\/\//i.test(t) || /^mailto:/i.test(t)) return t;
  return null;
}
