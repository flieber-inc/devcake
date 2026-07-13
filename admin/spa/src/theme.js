// Theme preference: "light" | "dark" | "system", persisted to localStorage.
// index.html applies the initial class pre-paint; this module keeps it in sync.

const KEY = "devcake-theme";
const mq = window.matchMedia("(prefers-color-scheme: dark)");
const listeners = new Set();

export function getTheme() {
  const v = localStorage.getItem(KEY);
  return v === "light" || v === "dark" ? v : "system";
}

function apply() {
  const pref = getTheme();
  const dark = pref === "dark" || (pref === "system" && mq.matches);
  document.documentElement.classList.toggle("dark", dark);
  listeners.forEach((fn) => fn(pref));
}

export function setTheme(pref) {
  if (pref === "system") localStorage.removeItem(KEY);
  else localStorage.setItem(KEY, pref);
  apply();
}

export function onThemeChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

mq.addEventListener("change", () => { if (getTheme() === "system") apply(); });
apply();
