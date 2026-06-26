// Drive the REAL shim subprocess with the MOCK engine (no DeepSeek). Use an acceptanceTest
// that is `true` (always passes) vs `false` (always fails) to exercise both harness paths
// without a real codebase. Proves the plumbing end-to-end (request field -> shim -> harness
// -> test run -> __HARNESS__ summary) and byte-inert-when-off.
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SHIM = path.join(ROOT, "engine", "run-lane.mjs");
const DIST = path.join(ROOT, "vendor", "reasonix-engine", "dist", "index.js");
let p = 0, f = 0; const chk = (c, m) => { if (c) { p++; console.log("  ok  ", m); } else { f++; console.log("  FAIL", m); } };

function runShim(env, req) {
  const r = spawnSync(process.execPath, [SHIM], {
    input: JSON.stringify(req),
    env: { ...process.env, REASONIX_ENGINE_DIST: DIST, REASONIX_ENGINE_MOCK: "1", ...env },
    encoding: "utf8", timeout: 60000,
  });
  const line = (r.stdout || "").trim().split("\n").filter(Boolean).pop() || "{}";
  return JSON.parse(line);
}
const baseReq = { prompt: "do x", system: "", rootDir: ROOT, model: "deepseek-v4-flash", maxIterPerTurn: 1 };

// off: normal single-run reply, NO __HARNESS__ prefix (byte-inert)
let out = runShim({}, { ...baseReq });
chk(typeof out.text === "string" && !out.text.startsWith("__HARNESS__:"), "flag off: normal reply, no harness");

// on + acceptanceTest 'true' (passes) -> __HARNESS__:pass:1
out = runShim({ REASONIX_LANE_HARNESS: "1" }, { ...baseReq, acceptanceTest: "true" });
chk(out.text.startsWith("__HARNESS__:pass:"), "harness on + passing test -> pass summary");

// on + acceptanceTest 'false' (always fails, same errorSig) -> stagnates quickly, not pass
out = runShim({ REASONIX_LANE_HARNESS: "1" }, { ...baseReq, acceptanceTest: "false", harnessMaxAttempts: 5 });
chk(out.text.startsWith("__HARNESS__:") && !out.text.startsWith("__HARNESS__:pass"), "always-failing test -> non-pass (stagnated/exhausted)");

console.log(`\n${p} passed, ${f} failed`); process.exit(f ? 1 : 0);
