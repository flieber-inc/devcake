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
// POST that answers with a FILE: fetch → Blob → synthetic <a download> click.
// The server names the file via Content-Disposition (the client only falls
// back). Auth rides the browser session like every other call.
export async function download(path, body) {
  const r = await fetch(`/api/v1${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-DevCake-Request": "1" },
    body: JSON.stringify(body ?? {}),
  });
  if (!r.ok) await fail(r);
  const name = ((r.headers.get("Content-Disposition") || "")
    .match(/filename="?([^";]+)"?/) || [])[1] || "devcake-export.yaml";
  const url = URL.createObjectURL(await r.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  return name;
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
