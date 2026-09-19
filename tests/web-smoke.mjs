import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const html = readFileSync(join(root, "web", "index.html"), "utf8");
const required = [
  "data-testid=\"task-list\"",
  "data-testid=\"plan-btn\"",
  "data-testid=\"when-to-run\"",
  "data-testid=\"deltas\"",
  "data-testid=\"replan-btn\"",
  "data-testid=\"cost-headline\"",
  "data-testid=\"compare-btn\"",
  "data-testid=\"compare-table\"",
  "data-testid=\"timeline\"",
  "data-testid=\"device-presets\"",
  "data-testid=\"self-consumption-delta\"",
  "data-testid=\"replan-changes\"",
  "/api/plan",
  "/api/replan",
  "/api/compare",
  "self_consumption_kwh",
  "deltaClass",
];
const missing = required.filter((token) => !html.includes(token));
if (missing.length) {
  console.error("web/index.html missing:", missing.join(", "));
  process.exit(1);
}
if (!html.includes('metric === "self_consumption_kwh"') || !html.includes('return n >= 0 ? "ok"')) {
  console.error("self-consumption increase must be classified as ok, not cost/red");
  process.exit(1);
}
console.log("web smoke ok");
