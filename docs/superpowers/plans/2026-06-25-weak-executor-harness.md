# Weak-Executor Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make DeepSeek-flash lanes actually COMPLETE hard agentic work via a progressing-but-cheap retry harness, with Opus only planning + reviewing short results — fixing the measured 19M-token / 97%-cache-read paradox.

**Architecture:** Three flag-gated components: C1 a PREFIX_GUIDE point that makes the Opus orchestrator emit complete-subtask lanes with instance-level specs; C2 a SHIM-level acceptance-test retry loop (the engine has no internal verify loop, so the shim runs the lane, runs the lane's acceptanceTest, and on failure re-runs with a SHORT lesson + fresh small context — lesson-only carry, a progress-gate that stops on stagnation, and a budgetUsd cap); C3 a SHORT structured lane result so Opus reviews/escalates cheaply instead of re-reading raw files.

**Tech Stack:** Node ESM shim (`engine/run-lane.mjs`), Python gateway (`reasonix-native-gateway.py`), Python hook (`hooks/reasonix-workflow.py`). Engine primitives reused (NO bundle edit): `CacheFirstLoop` (constructor opts incl `budgetUsd`, `maxIterPerTurn`), `buildCodeToolset` (the file/shell ACI tools the lane already has). Tests: repo `tests/test-*.py` (`main()` returning 0/1) + node tests run with `node`.

## Global Constraints

- **Default OFF + byte-inert when off.** With every new flag unset, the lane request, assembled prompt, and lane reply must be byte-identical to today. A flag-off path that changes one byte breaks the 96-99% prefix cache and is a Critical defect. (spec hard constraint)
- **The harness MUST be CHEAPER than plain Opus, never more.** The retry loop must NOT accumulate history (Reflexion's quadratic-cost warning) — it uses lesson-only carry + a progress-gate + a `budgetUsd` cap. (spec)
- **No vendored-engine bundle edit** (`vendor/reasonix-engine/dist/index.js` untouched) — C2 builds the retry loop at the SHIM, reusing `CacheFirstLoop` opts.
- **Measure the TRUE cost including the Opus orchestrator session** (orchestrator `.jsonl` message usage incl `cache_read_input_tokens` + the lane ledger) — NEVER report lane-only cost (that was the hidden-cost mistake). ([[reasonix-orchestrator-cost-paradox]])
- **env_truthy(*names, default="")** — positional args are env var NAMES; pass the default as the `default=` keyword. (process-lessons)
- **Tests must be e2e where the real lane path differs from unit** — the gateway has `/v1/messages` and `/v1/chat/completions`; the shim is a spawned subprocess. (process-lessons)

---

### Task 1: C2a — shim acceptance-test retry loop (the core lever)

**Files:**
- Create: `engine/lane-harness.mjs` (the retry-loop logic, exported + unit-testable without DeepSeek)
- Modify: `engine/run-lane.mjs` (call the harness when `req.acceptanceTest` + flag set; else today's single `loop.step`)
- Test: `tests/test-lane-harness.mjs` (node)

**Interfaces:**
- Consumes: `req.acceptanceTest` (a shell command string, e.g. `"bun test X"`), `req.harnessMaxAttempts` (int, default 4), `process.env.REASONIX_LANE_HARNESS` (gate).
- Produces: `runHarness({ runAttempt, runTest, maxAttempts })` → `{ status: 'pass'|'stagnated'|'exhausted', attempts, lastLesson, testResult }`. `runAttempt(lesson)` is an injected async fn that does one lane turn (returns the assistant text); `runTest()` is an injected async fn that runs the acceptanceTest and returns `{ ok: bool, failCount: int, errorSig: string }`. This injection keeps the loop logic PURE and unit-testable (no DeepSeek, no shell in the test).

The loop (the spec's C2 mechanics):
- attempt 1: `runAttempt(null)`, then `runTest()`. If `ok` → return pass.
- on fail: derive a SHORT lesson from the test result; record `failCount`+`errorSig`.
- **progress-gate:** before re-trying, compare to the PREVIOUS attempt — retry ONLY if `failCount` dropped OR `errorSig` is DIFFERENT. If neither (stagnation) → return `{status:'stagnated'}` immediately (no useless spin).
- **lesson-only carry:** the next `runAttempt(lesson)` gets ONLY the short lesson (the caller builds its context fresh — spec + current code on disk + lesson — NOT accumulated history).
- stop at `maxAttempts` → `{status:'exhausted'}`.

- [ ] **Step 1: Write the failing test** `tests/test-lane-harness.mjs`

```javascript
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
```

- [ ] **Step 2: Run it, verify it fails**

Run: `node tests/test-lane-harness.mjs`
Expected: FAIL — `runHarness` not found.

- [ ] **Step 3: Implement** `engine/lane-harness.mjs`

```javascript
// Lane retry harness: a PROGRESSING, non-bloating, bounded acceptance-test loop.
// Reflexion-style (learn from the failed test) but lesson-only (no accumulated history,
// which Reflexion warns grows cost ~quadratically) + a progress-gate (stop on stagnation,
// not a blunt iteration cap) + the caller's budgetUsd cap. Pure logic; deps are injected
// so it is unit-testable without DeepSeek or a shell.
export async function runHarness({ runAttempt, runTest, maxAttempts = 4 }) {
  let prev = null;          // { failCount, errorSig } of the previous attempt
  let lesson = null;        // short lesson carried to the next attempt
  let testResult = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    await runAttempt(lesson);
    testResult = await runTest();
    if (testResult.ok) return { status: "pass", attempts: attempt, lastLesson: lesson, testResult };
    // progress-gate: did we make measurable progress vs the previous attempt?
    if (prev !== null) {
      const progressed = testResult.failCount < prev.failCount || testResult.errorSig !== prev.errorSig;
      if (!progressed) {
        return { status: "stagnated", attempts: attempt, lastLesson: lesson, testResult };
      }
    }
    // lesson-only carry: a SHORT lesson for the next attempt (caller rebuilds fresh context)
    lesson = `Previous attempt failed: ${testResult.failCount} test(s) failing — ${testResult.errorSig}. Fix the cause, do not repeat the same change.`;
    prev = { failCount: testResult.failCount, errorSig: testResult.errorSig };
  }
  return { status: "exhausted", attempts: maxAttempts, lastLesson: lesson, testResult };
}
```

- [ ] **Step 4: Run it, verify it passes**

Run: `node tests/test-lane-harness.mjs`
Expected: `5 passed, 0 failed`.

- [ ] **Step 5: Wire it into the shim (gated, byte-inert off)** — in `engine/run-lane.mjs`, after the existing `for await (const ev of loop.step(...))` block (line ~208-217), the lane currently runs ONCE. Add: when `process.env.REASONIX_LANE_HARNESS` is truthy AND `req.acceptanceTest` is a non-empty string, wrap the lane run in `runHarness`. Import the spawn/test helper and the harness:

```javascript
import { runHarness } from "./lane-harness.mjs";
```

Replace the single-run section with a harness-or-single decision. Keep the ORIGINAL single-run path EXACTLY when the flag is off or there's no acceptanceTest (byte-inert):

```javascript
const _harnessOn = (process.env.REASONIX_LANE_HARNESS || "").trim().toLowerCase() === "1"
  || (process.env.REASONIX_LANE_HARNESS || "").trim().toLowerCase() === "true";
const _acceptance = typeof req.acceptanceTest === "string" ? req.acceptanceTest.trim() : "";

if (_harnessOn && _acceptance) {
  const { execSync } = await import("node:child_process");
  const runTest = async () => {
    try {
      execSync(_acceptance, { cwd: rootDir, stdio: "pipe", timeout: 120000 });
      return { ok: true, failCount: 0, errorSig: "" };
    } catch (e) {
      const out = String(e.stdout || "") + String(e.stderr || "");
      const m = out.match(/(\d+)\s+fail/i);            // e.g. bun "N fail"
      const failCount = m ? parseInt(m[1], 10) : 1;
      const sig = (out.match(/[A-Za-z][\w./-]*:\d+/) || [out.slice(0, 40)])[0]; // first file:line or head
      return { ok: false, failCount, errorSig: String(sig) };
    }
  };
  const runAttempt = async (lesson) => {
    const p = lesson ? `${String(req.prompt ?? "")}\n\nLESSON FROM LAST ATTEMPT (apply it, do not repeat the same edit):\n${lesson}` : String(req.prompt ?? "");
    // fresh loop per attempt = lesson-only carry (no accumulated history -> no quadratic cost)
    const attemptLoop = new CacheFirstLoop({
      client, prefix, tools: toolset.tools, model: req.model, stream: true,
      session: undefined, maxIterPerTurn: req.maxIterPerTurn ?? 50,
      maxOutputTokens: req.maxOutputTokens ?? undefined,
      budgetUsd: typeof req.budgetUsd === "number" ? req.budgetUsd : undefined,
    });
    let t = "";
    for await (const ev of attemptLoop.step(p)) {
      if (ev.role === "assistant_final") { t = ev.content ?? ""; if (ev.stats) stats = ev.stats; }
      else if (ev.role === "error") throw new Error(ev.content || "engine error");
      else if (ev.role === "done") break;
    }
    return t;
  };
  const _h = await runHarness({ runAttempt, runTest, maxAttempts: req.harnessMaxAttempts ?? 4 });
  text = `__HARNESS__:${_h.status}:${_h.attempts}:${(_h.lastLesson || "").slice(0, 200)}`;
}
```

(The `text` carries a structured harness summary that Task 3 / the gateway turns into the short lane result. The single-run path above it is UNCHANGED.)

- [ ] **Step 6: Shim parses + byte-inert when off**

Run: `node --check engine/run-lane.mjs` (valid). Confirm off-path: `REASONIX_LANE_HARNESS` unset → the new `if` is skipped → original single-run runs (read the code to confirm the original `for await` block is intact and reached).

- [ ] **Step 7: Commit**

```bash
git add engine/lane-harness.mjs engine/run-lane.mjs tests/test-lane-harness.mjs
git commit -m "feat(harness): C2 — shim acceptance-test retry loop (lesson-only + progress-gate + budget, default off)"
```

---

### Task 2: C3 — lane returns a SHORT structured result; gateway surfaces it

**Files:**
- Modify: `reasonix-native-gateway.py` — the shim request dict (line ~2111, add `acceptanceTest`/`budgetUsd`/`harnessMaxAttempts` when present) + a helper that turns the shim's `__HARNESS__:...` text into a short structured lane reply.
- Test: `tests/test-harness-result.py`

**Interfaces:**
- Consumes: the shim's `text` field which, in harness mode, is `__HARNESS__:<status>:<attempts>:<lesson>` (from Task 1).
- Produces: `parse_harness_result(text) -> dict | None` returning `{status, attempts, lesson}` for a harness text, else None; and `harness_lane_reply(parsed) -> str` returning a SHORT structured summary string (status + attempts + a "ESCALATE" marker when status is stagnated/exhausted) for the orchestrator to read. Gated by the same on/off as the shim (the gateway only forwards `acceptanceTest` when `CLAUDE_REASONIX_GATEWAY_LANE_HARNESS` is on; default off → byte-inert).

- [ ] **Step 1: Write the failing test** `tests/test-harness-result.py`

```python
#!/usr/bin/env python3
"""C3: parse the shim's __HARNESS__ text into a SHORT structured result; a stagnated/
exhausted lane is marked ESCALATE so Opus reviews only failures, never raw files."""
import importlib.util, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(gw)
_P = _F = 0
def chk(c, m):
    global _P, _F
    if c: _P += 1; print(f"  ok   {m}")
    else: _F += 1; print(f"  FAIL {m}")
def main():
    # non-harness text -> None (passthrough, byte-inert)
    chk(gw.parse_harness_result("just a normal lane reply") is None, "normal text -> None")
    # pass
    p = gw.parse_harness_result("__HARNESS__:pass:2:")
    chk(p == {"status": "pass", "attempts": 2, "lesson": ""}, "parses pass")
    r = gw.harness_lane_reply(p)
    chk("pass" in r and "ESCALATE" not in r, "pass reply has no ESCALATE")
    # stagnated -> ESCALATE marker for Opus
    p = gw.parse_harness_result("__HARNESS__:stagnated:3:error at x.ts:42")
    chk(p["status"] == "stagnated", "parses stagnated")
    r = gw.harness_lane_reply(p)
    chk("ESCALATE" in r and "x.ts:42" in r, "stagnated reply carries ESCALATE + the lesson")
    # exhausted -> ESCALATE
    chk("ESCALATE" in gw.harness_lane_reply(gw.parse_harness_result("__HARNESS__:exhausted:4:")), "exhausted -> ESCALATE")
    # the reply is SHORT (the whole point — no raw files)
    chk(len(gw.harness_lane_reply(gw.parse_harness_result("__HARNESS__:pass:1:"))) < 200, "reply is short")
    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0
if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it, verify it fails**

Run: `python3 tests/test-harness-result.py`
Expected: FAIL — `parse_harness_result` not defined.

- [ ] **Step 3: Implement** in `reasonix-native-gateway.py`:

```python
def parse_harness_result(text: str) -> JSON | None:
    """Parse the shim's harness summary text `__HARNESS__:<status>:<attempts>:<lesson>`.
    Returns None for a normal (non-harness) reply so the gateway passes it through
    unchanged (byte-inert when the harness is off)."""
    if not isinstance(text, str) or not text.startswith("__HARNESS__:"):
        return None
    parts = text.split(":", 3)  # ['__HARNESS__', status, attempts, lesson]
    if len(parts) < 3:
        return None
    try:
        attempts = int(parts[2])
    except (TypeError, ValueError):
        attempts = 0
    return {"status": parts[1], "attempts": attempts, "lesson": parts[3] if len(parts) > 3 else ""}


def harness_lane_reply(parsed: JSON) -> str:
    """A SHORT structured lane reply for the orchestrator. A passed lane returns a terse
    OK; a stagnated/exhausted lane carries an ESCALATE marker + the lesson so Opus reviews
    ONLY the failures (never re-reading raw files — the 97% cache-read fix)."""
    st = parsed.get("status")
    att = parsed.get("attempts")
    if st == "pass":
        return f"LANE_OK: completed in {att} attempt(s), acceptance test green."
    return (f"LANE_ESCALATE: status={st} after {att} attempt(s). "
            f"Could not finish; orchestrator should take over this lane. Lesson: {parsed.get('lesson','')}")
```

Then at the shim-reply handling (where `text, usage = run_reasonix_acp(...)` result becomes the lane reply), add: if `parse_harness_result(text)` is not None, replace `text` with `harness_lane_reply(parsed)` BEFORE building the response. Gate the whole forwarding behind `_lane_harness_on()`:

```python
def _lane_harness_on() -> bool:
    return env_truthy("CLAUDE_REASONIX_GATEWAY_LANE_HARNESS",
                      "CLAUDE_CODEX_GATEWAY_LANE_HARNESS", default="0")
```

And in the shim request dict (line ~2111), forward the harness fields ONLY when on (so off = byte-identical request):

```python
        if _lane_harness_on():
            _at = lane_acceptance_test(messages)   # extract an acceptance test from the lane prompt, see below
            if _at:
                request["acceptanceTest"] = _at
                request["budgetUsd"] = env_float("CLAUDE_REASONIX_GATEWAY_LANE_BUDGET_USD",
                                                 "CLAUDE_CODEX_GATEWAY_LANE_BUDGET_USD", default=0.05)
                request["harnessMaxAttempts"] = env_int("CLAUDE_REASONIX_GATEWAY_LANE_MAX_ATTEMPTS",
                                                        "CLAUDE_CODEX_GATEWAY_LANE_MAX_ATTEMPTS", default=4)
```

`lane_acceptance_test(messages)` extracts a fenced acceptance command the orchestrator put in the lane prompt (C1 instructs it to). Minimal version: find a line of the form `ACCEPTANCE_TEST: <cmd>` in the raw lane task text:

```python
def lane_acceptance_test(messages: Any) -> str:
    txt = lane_task_text(messages)
    for line in txt.splitlines():
        s = line.strip()
        if s.upper().startswith("ACCEPTANCE_TEST:"):
            return s.split(":", 1)[1].strip()
    return ""
```

- [ ] **Step 4: Run it, verify it passes**

Run: `python3 tests/test-harness-result.py`
Expected: `7 passed, 0 failed`.

- [ ] **Step 5: Byte-inert + guard**

Confirm: with `CLAUDE_REASONIX_GATEWAY_LANE_HARNESS` unset, the request dict gets NO new fields and `parse_harness_result` returns None for normal text (the test's first case). Run `python3 tests/test-no-codex-leftovers.py` (PASS).

- [ ] **Step 6: Commit**

```bash
git add -f reasonix-native-gateway.py && git add tests/test-harness-result.py
git commit -m "feat(harness): C3 — short structured lane result + escalate-only; forward acceptanceTest gated"
```

---

### Task 3: C1 — PREFIX_GUIDE point: instance-level specs + complete-subtask fan-out

**Files:**
- Modify: `hooks/reasonix-workflow.py` — append point 11 to `PREFIX_GUIDE_TEXT` (current last point is 10).
- Test: `tests/test-prefix-guide-harness.py`

**Interfaces:**
- Consumes: `PREFIX_GUIDE_TEXT`, the existing `CLAUDE_REASONIX_WORKFLOW_PREFIX_GUIDE` gate.
- Produces: an additional advisory point (point 11) instructing the orchestrator to (a) make each lane a COMPLETE sub-task (draft+edit+verify, not just draft), (b) hand each lane an instance-level spec: 1-2 sentence plan + exact files + an `ACCEPTANCE_TEST: <cmd>` line (which the harness in Task 2 reads), and (c) NOT dump repo structure/summaries/few-shot (measured to hurt).

- [ ] **Step 1: Write the failing test** `tests/test-prefix-guide-harness.py`

```python
#!/usr/bin/env python3
"""C1: PREFIX_GUIDE point 11 must instruct instance-level specs + complete-subtask lanes
+ the ACCEPTANCE_TEST line the harness consumes, and must NOT have been removed when off."""
import importlib.util, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("h", ROOT / "hooks" / "reasonix-workflow.py")
h = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(h)
_P = _F = 0
def chk(c, m):
    global _P, _F
    if c: _P += 1; print(f"  ok   {m}")
    else: _F += 1; print(f"  FAIL {m}")
def main():
    g = h.PREFIX_GUIDE_TEXT
    chk("11." in g, "point 11 present")
    chk("ACCEPTANCE_TEST:" in g, "tells orchestrator to emit ACCEPTANCE_TEST line (harness reads it)")
    chk("instance" in g.lower() and ("complete" in g.lower() or "draft+" in g.lower() or "edit + verify" in g.lower()),
        "instructs instance-level specs + complete sub-task lanes")
    chk("do not" in g.lower() and ("repo" in g.lower() or "dump" in g.lower() or "few-shot" in g.lower()),
        "warns against repo-dump / few-shot (measured to hurt)")
    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0
if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it, verify it fails**

Run: `python3 tests/test-prefix-guide-harness.py`
Expected: FAIL — point 11 not present yet.

- [ ] **Step 3: Implement** — append to `PREFIX_GUIDE_TEXT` after point 10 (find the point-10 string ending and add this string literal before the closing `)`):

```python
    "11. HARD-TASK HARNESS — when a lane must EDIT code and pass tests (a real refactor/"
    "fix, not a read), make each lane a COMPLETE sub-task (it drafts + edits + verifies, "
    "not just drafts), and hand it an INSTANCE-LEVEL spec: a 1-2 sentence plan + the EXACT "
    "files it touches + one line `ACCEPTANCE_TEST: <shell command>` (e.g. "
    "`ACCEPTANCE_TEST: bun test path/x.test.ts`). The lane harness runs that command, and "
    "on failure makes the lane retry with a short lesson until the test passes or it "
    "stalls (then it returns LANE_ESCALATE for you to take over). Do NOT dump repo "
    "structure, file summaries, or few-shot examples into a lane — measured to HURT a weak "
    "executor (instance-level plan+files+test is what helps). Review the SHORT lane results "
    "(LANE_OK / LANE_ESCALATE); only take over the LANE_ESCALATE lanes yourself.\n"
```

- [ ] **Step 4: Run it, verify it passes**

Run: `python3 tests/test-prefix-guide-harness.py`
Expected: `4 passed, 0 failed`.

- [ ] **Step 5: Guide ordering + suite regression**

Run: `python3 tests/test-no-codex-leftovers.py` (PASS) and any existing `tests/test-*guide*.py` (still pass — point 11 added after 10, no reorder). Confirm `PREFIX_GUIDE_TEXT` is still gated by `CLAUDE_REASONIX_WORKFLOW_PREFIX_GUIDE` (point 11 rides the same gate; off → whole guide suppressed = byte-inert).

- [ ] **Step 6: Commit**

```bash
git add -f hooks/reasonix-workflow.py && git add tests/test-prefix-guide-harness.py
git commit -m "feat(harness): C1 — PREFIX_GUIDE point 11 (instance-level specs + ACCEPTANCE_TEST + complete-subtask lanes)"
```

---

### Task 4: e2e harness test over a real shim (the integration gate)

**Files:**
- Test: `tests/test-harness-e2e.mjs` (node — drives the REAL shim with the MOCK engine so no DeepSeek/cost, but exercises the real harness loop + acceptanceTest plumbing)

**Interfaces:**
- Consumes: the shim `engine/run-lane.mjs` with `REASONIX_ENGINE_MOCK=1` (already exists — deterministic reply, no DeepSeek) + `REASONIX_LANE_HARNESS=1` + a request carrying `acceptanceTest`.
- Produces: proof that, over the real spawned shim, the harness runs the acceptanceTest, retries on failure, and returns a `__HARNESS__:` summary — and that with the flag off the shim returns its normal single-run reply (byte-inert).

- [ ] **Step 1: Write the failing test** `tests/test-harness-e2e.mjs`

```javascript
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
```

- [ ] **Step 2: Run it, verify it fails**

Run: `node tests/test-harness-e2e.mjs`
Expected: FAIL (harness wiring from Task 1 must already be in; if Task 1 was committed this exercises it end-to-end — the test fails first only if run before Task 1). After Task 1+2 it should pass; this task ADDS the e2e gate.

- [ ] **Step 3: Make it pass**

If the e2e reveals a plumbing gap (e.g. the MOCK shim short-circuits before the harness, or the `text` shape differs), fix it in `engine/run-lane.mjs` — the MOCK path (`REASONIX_ENGINE_MOCK=1`) must still go through the harness when `REASONIX_LANE_HARNESS=1` + acceptanceTest. (The mock returns a fixed text per attempt; the acceptanceTest `true`/`false` drives the loop.) Re-run until `3 passed, 0 failed`.

- [ ] **Step 4: Full sweep**

Run every `tests/test-*.py` + `node tests/test-lane-harness.mjs` + `node tests/test-harness-e2e.mjs` + `python3 tests/test-no-codex-leftovers.py` — none may FAIL.

- [ ] **Step 5: Commit**

```bash
git add tests/test-harness-e2e.mjs engine/run-lane.mjs
git commit -m "test(harness): e2e over the real shim (mock engine) — harness loop + acceptanceTest + byte-inert off"
```

---

## Self-Review

- **Spec coverage:** C1 → Task 3; C2 (lesson-only + progress-gate + budget) → Task 1; C3 (short result + escalate) → Task 2; e2e measurement plumbing → Task 4. The real-cost measurement on the Bun task (incl Opus orchestrator) is the post-merge validation, noted in Global Constraints + the spec's "how we'll know" — done as a measured run after the harness ships, not a unit task.
- **Placeholder scan:** every code step has full code; the one extraction helper (`lane_acceptance_test`) is fully specified; no TBD.
- **Type consistency:** `runHarness({runAttempt,runTest,maxAttempts}) -> {status,attempts,lastLesson,testResult}`; `parse_harness_result(text)->dict|None`; `harness_lane_reply(parsed)->str`; `_lane_harness_on/_lane_harness_on -> bool`; the shim's `__HARNESS__:<status>:<attempts>:<lesson>` shape is produced in Task 1 and parsed in Task 2 — consistent.
- **Token effect per task:** T1 only re-runs while progressing under a budget cap (cheaper than a blunt loop); T2 short result (cuts orchestrator re-read); T3 instance spec < repo dump; all byte-inert off. None regress cache when off.

## Execution note
The harness ships default-OFF. The decisive validation is a measured re-run of the HARD Bun task with the harness ON, reporting the TRUE total cost INCLUDING the Opus orchestrator session (`.jsonl` usage incl cache_read) vs the $42/19M baseline — never lane-only.
