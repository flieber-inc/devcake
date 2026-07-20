// Pure helpers for Markdown View — no browser / vite required.
import { isMarkdownPath, safeHref, stripYamlFrontmatter } from "../src/lib/markdown.js";

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

assert(isMarkdownPath("SKILL.md"), "SKILL.md is markdown");
assert(isMarkdownPath("refs/FOO.MD"), ".MD is markdown");
assert(isMarkdownPath("x.markdown"), ".markdown is markdown");
assert(!isMarkdownPath("script.py"), "py is not markdown");
assert(!isMarkdownPath(""), "empty path is not markdown");

const withFm = "---\nname: x\n---\n\n# Title\n";
assert(stripYamlFrontmatter(withFm) === "\n# Title\n", "strips leading frontmatter");
assert(stripYamlFrontmatter("# only\n") === "# only\n", "no-op without frontmatter");
const mid = "# hi\n\n---\nnot fm\n---\n";
assert(stripYamlFrontmatter(mid) === mid, "mid-body --- left alone");
const unclosed = "---\nname: x\n# no close\n";
assert(stripYamlFrontmatter(unclosed) === unclosed, "unclosed fence left alone");

assert(safeHref("https://example.com/a") === "https://example.com/a", "https ok");
assert(safeHref("http://example.com") === "http://example.com", "http ok");
assert(safeHref("mailto:a@b.c") === "mailto:a@b.c", "mailto ok");
assert(safeHref("javascript:alert(1)") === null, "javascript blocked");
assert(safeHref("data:text/html,x") === null, "data blocked");
assert(safeHref("../other.md") === null, "relative blocked");

console.log("markdown_helpers: ok");
