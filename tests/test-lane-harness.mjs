// Pure loop logic — inject fake runAttempt/runTest so no DeepSeek/shell needed.
import assert from "node:assert";
const { runHarness } = await import("../engine/lane-harness.mjs");
let p = 0, f = 0;
const chk = (c, m) => { if (c) { p++; console.log("  ok  ", m); } else { f++; console.log("  FAIL", m); } };

// 1. passes first try
let r = await runHarness({
  runAttempt: async () => "did it",
  runTest: async () => ({ ok: true, failCount: 0, errorSig: "" }),
  maxAttempts: 4,
});
chk(r.status === "pass" && r.attempts === 1, "pass on attempt 1");

// 2. fails then passes (progress: failCount drops) -> retries -> pass
let calls = 0;
r = await runHarness({
  runAttempt: async (lesson) => { calls++; return "try"; },
  runTest: async () => calls < 2 ? { ok: false, failCount: 3, errorSig: "E1" }
                                 : { ok: true, failCount: 0, errorSig: "" },
  maxAttempts: 4,
});
chk(r.status === "pass" && r.attempts === 2, "retries when progressing, then passes");

// 3. STAGNATION: same failCount + same errorSig two attempts -> stop early (NOT maxAttempts)
r = await runHarness({
  runAttempt: async () => "try",
  runTest: async () => ({ ok: false, failCount: 3, errorSig: "SAME" }),
  maxAttempts: 9,
});
chk(r.status === "stagnated" && r.attempts === 2, "stops on stagnation at attempt 2 (no useless spin)");

// 4. PROGRESS via different error each time, never passes -> exhausts maxAttempts
let i = 0;
r = await runHarness({
  runAttempt: async () => "try",
  runTest: async () => ({ ok: false, failCount: 3, errorSig: "E" + (i++) }),
  maxAttempts: 3,
});
chk(r.status === "exhausted" && r.attempts === 3, "progressing-but-unsolved exhausts maxAttempts");

// 5. lesson is passed forward (not null on retry)
let gotLesson = null;
await runHarness({
  runAttempt: async (lesson) => { gotLesson = lesson; return "t"; },
  runTest: async () => ({ ok: false, failCount: 2, errorSig: "X" }),
  maxAttempts: 2,
});
chk(typeof gotLesson === "string" && gotLesson.length > 0, "retry attempt receives a non-empty lesson");

console.log(`\n${p} passed, ${f} failed`); process.exit(f ? 1 : 0);
