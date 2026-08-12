import assert from "node:assert/strict";
import { liveMission, runsListPath } from "../src/lib/missionIdentity.js";

let failed = 0;
const check = (name, fn) => {
  try {
    fn();
    console.log(`  ✓ ${name}`);
  } catch (e) {
    failed += 1;
    console.log(`  ✗ ${name}: ${e.message}`);
  }
};

check("drawer fetch qualifies pmo_ref so colliding keys stay apart", () => {
  const path = runsListPath({ key: "DEV-1", instance: "eng" });
  assert.match(path, /mission_key=DEV-1/);
  assert.match(path, /pmo_ref=eng/);
});

check("liveMission prefers the projected row over the click-time snapshot", () => {
  const open = { instance: "eng", pmo_id: "1", labels: ["DEVCAKE"] };
  const rows = [
    { instance: "cs", pmo_id: "1", labels: ["DEVCAKE"] },
    { instance: "eng", pmo_id: "1", labels: ["DEVCAKE", "DEVCAKE-SKIP"] },
  ];
  assert.deepEqual(liveMission(rows, open).labels, ["DEVCAKE", "DEVCAKE-SKIP"]);
});

check("liveMission keeps the snapshot if the row left the board", () => {
  const open = { instance: "eng", pmo_id: "9", labels: ["DEVCAKE"] };
  assert.equal(liveMission([], open), open);
});

if (failed) {
  console.error(`mission_identity.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("mission_identity.mjs: all checks passed");
