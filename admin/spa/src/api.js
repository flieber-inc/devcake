// Thin fetch helpers. Basic auth rides the browser session (nginx gate).
export async function get(path) {
  const r = await fetch(`/api/v1${path}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
export async function getText(path) {
  const r = await fetch(`/api/v1${path}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
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
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
