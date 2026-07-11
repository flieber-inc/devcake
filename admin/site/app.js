// DevCake admin shell (M0): tabs, health strip, outbound buttons.
// Replaced by the Vite/React SPA when the Config tab lands (M6, docs/11).

const cfg = window.DEVCAKE || {};
document.getElementById("open-dagu").href = cfg.daguUrl || "http://localhost:8525";
document.getElementById("open-oo").href = cfg.ooUrl || "http://localhost:5080";

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t === tab));
    document.querySelectorAll(".panel").forEach(p =>
      p.classList.toggle("active", p.id === `tab-${tab.dataset.tab}`));
  });
}

async function refreshHealth() {
  let health = {};
  try {
    const resp = await fetch("/api/v1/health", { cache: "no-store" });
    if (resp.ok) health = await resp.json();
  } catch { /* app unreachable: everything stays/goes red below */ }
  for (const el of document.querySelectorAll(".dot")) {
    const ok = health[el.dataset.c];
    el.classList.toggle("ok", ok === true);
    el.classList.toggle("bad", ok === false || !("app" in health));
    el.classList.toggle("pending", false);
  }
}
refreshHealth();
setInterval(refreshHealth, 10000);
