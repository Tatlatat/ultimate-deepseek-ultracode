// Test the one-shot Node shim engine/run-lane.mjs I/O contract WITHOUT DeepSeek.
// With REASONIX_ENGINE_MOCK=1 the shim short-circuits to a deterministic reply,
// so this verifies the stdin->stdout JSON contract the gateway depends on.
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const SHIM = path.join(ROOT, "engine", "run-lane.mjs");

const req = JSON.stringify({
  prompt: "say hi",
  system: "you are a worker",
  rootDir: ROOT,
  model: "deepseek-v4-flash",
  maxIterPerTurn: 1,
});

const r = spawnSync("node", [SHIM], {
  input: req,
  env: { ...process.env, REASONIX_ENGINE_MOCK: "1" },
  encoding: "utf8",
});

if (r.status !== 0) {
  console.error("FAIL: shim exit", r.status, r.stderr);
  process.exit(1);
}

let out;
try {
  out = JSON.parse(r.stdout.trim().split("\n").pop());
} catch (e) {
  console.error("FAIL: shim stdout not JSON:", JSON.stringify(r.stdout), e.message);
  process.exit(1);
}

for (const k of ["text", "usage", "cost_usd"]) {
  if (!(k in out)) {
    console.error("FAIL: missing top-level key", k, out);
    process.exit(1);
  }
}
for (const k of [
  "prompt_tokens",
  "completion_tokens",
  "prompt_cache_hit_tokens",
  "prompt_cache_miss_tokens",
]) {
  if (!(k in out.usage)) {
    console.error("FAIL: usage missing", k, out.usage);
    process.exit(1);
  }
}
if (typeof out.text !== "string" || out.text.length === 0) {
  console.error("FAIL: text must be a non-empty string", out.text);
  process.exit(1);
}
if (typeof out.cost_usd !== "number") {
  console.error("FAIL: cost_usd must be a number", out.cost_usd);
  process.exit(1);
}

// The mock honors injected values so the gateway-side tests can assert real-ish
// numbers; verify the override path works here too.
const r2 = spawnSync("node", [SHIM], {
  input: req,
  env: {
    ...process.env,
    REASONIX_ENGINE_MOCK: "1",
    REASONIX_ENGINE_MOCK_TEXT: "PONG",
    REASONIX_ENGINE_MOCK_COST: "0.000123",
    REASONIX_ENGINE_MOCK_PROMPT_TOKENS: "100",
    REASONIX_ENGINE_MOCK_COMPLETION_TOKENS: "4",
    REASONIX_ENGINE_MOCK_CACHE_HIT_TOKENS: "90",
    REASONIX_ENGINE_MOCK_CACHE_MISS_TOKENS: "10",
  },
  encoding: "utf8",
});
if (r2.status !== 0) {
  console.error("FAIL: shim (override) exit", r2.status, r2.stderr);
  process.exit(1);
}
const out2 = JSON.parse(r2.stdout.trim().split("\n").pop());
if (out2.text !== "PONG") {
  console.error("FAIL: mock text override not honored", out2.text);
  process.exit(1);
}
if (out2.cost_usd !== 0.000123) {
  console.error("FAIL: mock cost override not honored", out2.cost_usd);
  process.exit(1);
}
if (out2.usage.prompt_tokens !== 100 || out2.usage.completion_tokens !== 4) {
  console.error("FAIL: mock token overrides not honored", out2.usage);
  process.exit(1);
}
if (out2.usage.prompt_cache_hit_tokens !== 90 || out2.usage.prompt_cache_miss_tokens !== 10) {
  console.error("FAIL: mock cache token overrides not honored", out2.usage);
  process.exit(1);
}

// Regression: an engine error event sets content:"" with the real message in
// ev.error (verified vs the real engine: vendor dist 39244/40412/40607). The shim
// must surface ev.error to stderr + exit 3 — NOT the bare "engine error: unknown".
// This was the root cause of "run-lane: engine error: unknown".
const ERR_MSG = "Authentication failed (DeepSeek 401): your api key is invalid";
const r3 = spawnSync("node", [SHIM], {
  input: req,
  env: { ...process.env, REASONIX_ENGINE_MOCK: "1", REASONIX_ENGINE_MOCK_ERROR: ERR_MSG },
  encoding: "utf8",
});
if (r3.status !== 3) {
  console.error("FAIL: error event should exit 3, got", r3.status, r3.stderr);
  process.exit(1);
}
if (!r3.stderr.includes(ERR_MSG)) {
  console.error("FAIL: stderr must carry the real ev.error message, got:", JSON.stringify(r3.stderr));
  process.exit(1);
}
if (r3.stderr.includes("engine error: unknown")) {
  console.error("FAIL: regressed — shim printed the bare 'unknown' instead of ev.error");
  process.exit(1);
}

console.log("PASS: engine shim I/O contract");
