// Prove the shim exits promptly (no dangling-handle hang, Gap-2) and that the
// flush-then-exit callback does NOT truncate the output.
//
// Uses REASONIX_ENGINE_MOCK=1 so no DeepSeek is needed.
// Three assertions per case:
//   1. spawnSync returns within the timeout (process exited — not hung)
//   2. stdout is complete parseable JSON (flush-before-exit, no truncation)
//   3. shape and known field values are byte-identical to what was written
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SHIM = path.join(ROOT, "engine", "run-lane.mjs");
const DIST = path.join(ROOT, "vendor", "reasonix-engine", "dist", "index.js");

let p = 0, f = 0;
const chk = (cond, msg) => {
  if (cond) { p++; console.log("  ok  ", msg); }
  else { f++; console.log("  FAIL", msg); }
};

const PROMPT_TIMEOUT = 10_000; // 10 s — a hung shim would never return; the old leak was 9 min

function runShim(env, req) {
  const start = Date.now();
  const r = spawnSync(process.execPath, [SHIM], {
    input: JSON.stringify(req),
    env: {
      ...process.env,
      REASONIX_ENGINE_DIST: DIST,
      REASONIX_ENGINE_MOCK: "1",
      ...env,
    },
    encoding: "utf8",
    timeout: PROMPT_TIMEOUT,
  });
  const elapsed = Date.now() - start;
  return { r, elapsed };
}

const baseReq = {
  prompt: "write a function that adds two numbers",
  system: "",
  rootDir: ROOT,
  model: "deepseek-v4-flash",
  maxIterPerTurn: 1,
};

// ── Case 1: non-harness mock lane ─────────────────────────────────────────────
// The final real-path stdout.write now has a flush-then-exit callback. Even with
// MOCK=1 and no harness, after writing the mock output the mock path calls
// process.exit(0) explicitly. Confirm the process exits promptly with complete JSON.
console.log("\nCase 1: non-harness mock lane");
{
  const { r, elapsed } = runShim({}, { ...baseReq });
  chk(!r.error, `shim exited cleanly (no spawnSync error: ${r.error?.message ?? "none"})`);
  chk(elapsed < PROMPT_TIMEOUT, `shim returned promptly (${elapsed}ms < ${PROMPT_TIMEOUT}ms)`);
  let out;
  try { out = JSON.parse((r.stdout || "").trim().split("\n").filter(Boolean).pop() || "{}"); }
  catch { out = null; }
  chk(out !== null, "stdout is complete parseable JSON (no truncation)");
  chk(typeof out?.text === "string", "output has text field");
  chk(typeof out?.usage === "object" && out.usage !== null, "output has usage object");
  chk(typeof out?.cost_usd === "number", "output has cost_usd number");
}

// ── Case 2: harness on + passing acceptance test ──────────────────────────────
// This is the real-path stdout.write (via runHarness → __HARNESS__ text).
// The shim must exit promptly and output must be complete.
console.log("\nCase 2: harness on + passing acceptance test (real-path exit)");
{
  const { r, elapsed } = runShim(
    { REASONIX_LANE_HARNESS: "1" },
    { ...baseReq, acceptanceTest: "true" },
  );
  chk(!r.error, `shim exited cleanly (no spawnSync error: ${r.error?.message ?? "none"})`);
  chk(elapsed < PROMPT_TIMEOUT, `shim returned promptly (${elapsed}ms < ${PROMPT_TIMEOUT}ms)`);
  let out;
  try { out = JSON.parse((r.stdout || "").trim().split("\n").filter(Boolean).pop() || "{}"); }
  catch { out = null; }
  chk(out !== null, "stdout is complete parseable JSON (no truncation)");
  // text must be the full __HARNESS__ prefix — truncation would break this parse or shorten the string
  chk(
    typeof out?.text === "string" && out.text.startsWith("__HARNESS__:pass:"),
    `output text is the full __HARNESS__:pass:... string (got: ${String(out?.text).slice(0, 60)})`,
  );
  chk(typeof out?.usage === "object" && out.usage !== null, "output has usage object");
  chk(typeof out?.cost_usd === "number", "output has cost_usd number");
}

// ── Case 3: harness on + failing acceptance test ──────────────────────────────
// Exercises the stagnated/exhausted path; still must exit promptly with complete JSON.
console.log("\nCase 3: harness on + always-failing acceptance test");
{
  const { r, elapsed } = runShim(
    { REASONIX_LANE_HARNESS: "1" },
    { ...baseReq, acceptanceTest: "false", harnessMaxAttempts: 3 },
  );
  chk(!r.error, `shim exited cleanly (no spawnSync error: ${r.error?.message ?? "none"})`);
  chk(elapsed < PROMPT_TIMEOUT, `shim returned promptly (${elapsed}ms < ${PROMPT_TIMEOUT}ms)`);
  let out;
  try { out = JSON.parse((r.stdout || "").trim().split("\n").filter(Boolean).pop() || "{}"); }
  catch { out = null; }
  chk(out !== null, "stdout is complete parseable JSON (no truncation)");
  chk(
    typeof out?.text === "string" && out.text.startsWith("__HARNESS__:") && !out.text.startsWith("__HARNESS__:pass"),
    `output text is non-pass __HARNESS__ summary (got: ${String(out?.text).slice(0, 60)})`,
  );
}

// ── Case 4: output JSON shape is byte-identical (only exit timing changes) ────
// Compare mock output field keys and types against the spec in the shim header comment.
console.log("\nCase 4: output JSON shape byte-identical to spec");
{
  const mockText = "custom mock text for shape test";
  const { r } = runShim(
    { REASONIX_ENGINE_MOCK_TEXT: mockText, REASONIX_ENGINE_MOCK_COST: "0.005" },
    { ...baseReq },
  );
  let out;
  try { out = JSON.parse((r.stdout || "").trim().split("\n").filter(Boolean).pop() || "{}"); }
  catch { out = null; }
  chk(out !== null, "output is parseable JSON");
  chk(out?.text === mockText, `text field matches env override (got: ${String(out?.text).slice(0, 60)})`);
  const usageKeys = ["prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cache_hit_ratio"];
  chk(
    usageKeys.every((k) => k in (out?.usage ?? {})),
    `usage has all required keys: ${usageKeys.join(", ")}`,
  );
  chk(out?.cost_usd === 0.005, `cost_usd matches env (${out?.cost_usd})`);
}

console.log(`\n${p} passed, ${f} failed`);
process.exit(f ? 1 : 0);
