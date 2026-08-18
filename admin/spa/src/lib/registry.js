// Adapter registry (GET /api/v1/connections/registry): which PMO systems and
// forges this DevCake build knows, plus their secret shapes. Fetched once;
// the static FALLBACK is a pinned mirror of the Python registry projection
// (ADR-0034) so the UI renders before (or without) the response. Pin test:
// app/tests/test_spa_registry_fallback.py — do not hand-edit the JSON
// without re-running that pin against connections_registry().
import { get } from "../api.js";
import FALLBACK from "./registry_fallback.json";

let cached = null;
let inflight = null;

export function getRegistry() {
  return cached || FALLBACK;
}

export function loadRegistry() {
  if (cached) return Promise.resolve(cached);
  if (!inflight) {
    inflight = get("/connections/registry")
      .then((r) => (cached = r))
      .catch(() => (cached = FALLBACK));
  }
  return inflight;
}
