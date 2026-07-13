// Immutable dot-path helpers for the config draft trees.
// Paths look like "cfg.repo.url", "devTypes.junior-dev.model",
// "assignments.PLAN.extra_cli_args". Dev type names contain no dots.

export function getIn(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
}

export function setIn(obj, path, value) {
  const keys = path.split(".");
  const root = Array.isArray(obj) ? [...obj] : { ...obj };
  let cur = root;
  for (let i = 0; i < keys.length - 1; i++) {
    const k = keys[i];
    const child = cur[k];
    cur[k] = child == null ? {} : Array.isArray(child) ? [...child] : { ...child };
    cur = cur[k];
  }
  cur[keys[keys.length - 1]] = value;
  return root;
}

const isPlainObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

// Leaf-level diff between two trees. Arrays are atomic leaves (they back
// textareas, so the line-set is the field). Returns [{path, old, new}].
export function diffLeaves(a, b, base = "") {
  const out = [];
  const keys = new Set([...Object.keys(a ?? {}), ...Object.keys(b ?? {})]);
  for (const k of keys) {
    const va = a?.[k];
    const vb = b?.[k];
    const path = base ? `${base}.${k}` : k;
    if (isPlainObject(va) && isPlainObject(vb)) {
      out.push(...diffLeaves(va, vb, path));
    } else if (JSON.stringify(va) !== JSON.stringify(vb)) {
      out.push({ path, old: va, new: vb });
    }
  }
  return out;
}

// Does the immediate parent container of `path` exist in `tree`?
// (Used by rebase: an edit under a deleted dev type is silently dropped.)
export function containerExists(tree, path) {
  const parent = path.split(".").slice(0, -1).join(".");
  return parent === "" || getIn(tree, parent) != null;
}
