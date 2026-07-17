// Thin fetch helpers. Basic auth rides the browser session (nginx gate).

// FastAPI errors carry a JSON `detail` — a string, or pydantic's list of
// {loc, msg}. Surface those as readable text instead of the raw JSON blob.
async function fail(r) {
  const text = await r.text();
  let msg = text;
  try {
    const d = JSON.parse(text).detail;
    if (typeof d === "string") msg = d;
    else if (Array.isArray(d))
      msg = d
        .map((e) => {
          const loc = (e.loc || []).filter((p) => p !== "body").join(".");
          return loc ? `${loc}: ${e.msg}` : e.msg;
        })
        .join(" · ");
  } catch { /* not JSON — keep the raw body */ }
  // `.status` lets callers branch on the code (e.g. 409 → overwrite prompt)
  // without sniffing the formatted message string
  throw Object.assign(new Error(`${r.status} ${msg}`), { status: r.status });
}

export async function get(path) {
  const r = await fetch(`/api/v1${path}`, { cache: "no-store" });
  if (!r.ok) await fail(r);
  return r.json();
}
export async function getText(path) {
  const r = await fetch(`/api/v1${path}`, { cache: "no-store" });
  if (!r.ok) await fail(r);
  return r.text();
}
export async function send(method, path, body) {
  const r = await fetch(`/api/v1${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-DevCake-Request": "1",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) await fail(r);
  return r.json();
}
