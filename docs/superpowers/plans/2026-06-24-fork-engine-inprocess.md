# Fork Engine In-Process Implementation Plan (Sub-project 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the fleet's DeepSeek engine the owner's fork (inspired by reasonix) called as a Node library via a one-shot shim, retiring the upstream-reasonix subprocess + the dist-surgery patch, so end users never install upstream reasonix.

**Architecture:** Two repos. (1) The FORK at `/Users/tatlatat/Documents/reasonix-fork` (TypeScript, ESM, v0.52.0) gets the one missing essence migrated in (the ephemeral-session trigger) + the two library exports the fleet needs, then is built to `dist/`. (2) The FLEET repo replaces its `reasonix acp` subprocess with a one-shot Node shim (`engine/run-lane.mjs`) that imports the fork's built `dist/index.js`, constructs `DeepSeekClient + ImmutablePrefix + CacheFirstLoop + buildCodeToolset`, runs ONE lane, and prints `{text, usage, cost}` JSON the gateway reads — preserving the exact lane-execution contract.

**Tech Stack:** Fork: TypeScript, tsup build, vitest. Fleet: Python gateway (`reasonix-native-gateway.py`), a new Node ESM shim, bash launcher, install.sh.

## Global Constraints

- **One-shot shim, NOT a persistent engine.** The current gateway spawns ONE `reasonix acp` subprocess PER lane (`subprocess.Popen` per request, 4 call sites) — in-memory cache is already NOT shared between lanes; DeepSeek's cache hits come from the SERVER-SIDE prefix cache (same prefix bytes → cached), kept warm by the existing `record_keepalive_prefix`/prime-gate. So a one-shot Node shim per lane is BEHAVIORALLY IDENTICAL to today and the lowest-risk seam. Do NOT build a persistent engine process — it would add in-memory cache the system does not depend on and a new lifecycle to manage.
- **stream:true is load-bearing.** The gateway's 180s watchdog/heartbeat requires streaming (MEMORY: claude-codex-180s-watchdog-nonstream). The shim must drive `loop.step()` (an `async *step(): AsyncGenerator<LoopEvent>`) and the gateway must keep emitting heartbeats while the shim runs.
- **The LoopEvent / TurnStats contract (verified in fork source):** `LoopEvent = { role: EventRole, content: string, stats?: TurnStats, cacheDiagnostic? }` (src/loop/types.ts:26). `EventRole` includes `assistant_final`, `done`, `error`. `TurnStats = { cost: number, usage: { promptTokens, completionTokens, promptCacheHitTokens, promptCacheMissTokens, cacheHitRatio, ... } }`. The shim maps these to the gateway's expected `{text, usage{prompt_tokens, completion_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens}, cost_usd}`.
- **Full code toolset:** `buildCodeToolset({ rootDir })` → `Promise<CodeToolset>` with `.tools` and `.semantic` (src/code/setup.ts:62). The shim must build it so lanes keep file/shell/semantic-search, matching current `reasonix acp` behavior.
- **Ephemeral == session:undefined in-process.** The shim passes `session: undefined` to CacheFirstLoop directly (zero disk I/O, no lane history bleed) — the env-var/config trigger is for the fork's CLI/acp path, not the in-process shim.
- **DeepSeek auth:** the fork's `DeepSeekClient` needs `DEEPSEEK_API_KEY` (env or constructor) and defaults `baseUrl` to `https://api.deepseek.com`. The gateway must pass `baseUrl` (via `DEEPSEEK_BASE_URL`) pointed at its own observability seam if it wants to keep tracing; otherwise direct is acceptable. Confirm which during Task 5.
- **The engine seam is the choke-point:** `run_reasonix_acp` (gateway) + the MCP dispatch (`reasonix-fleet-mcp.py:95-136`) + the self-heal reasonix-present check both call the engine — they must be cut over IN LOCKSTEP, never half-migrated.
- **No regression in the bench:** `runtime/realworld-bench.py` must keep ALL GATES PASS on real DeepSeek (review ~99%, fan-out ~90%+) through the new in-process engine — this is the decisive proof.
- **git-status discipline** after every subagent step.
- **Fork provenance** (reparent remote/author to Tatlatat) is owner-confirmed direction but the exact name is the owner's call — handled as its own task with an explicit owner gate.

---

### Task 1: Migrate the ephemeral essence into the fork + build it

**Files (in the FORK `/Users/tatlatat/Documents/reasonix-fork`):**
- Modify: `src/loop.ts` (session type widen, ~line 112)
- Create/Modify: `src/config.ts` (add `loadEphemeralSession()`)
- Modify: `src/cli/commands/acp.ts` (session trigger, ~line 194)
- Test: `src/__tests__/` (a new vitest for ephemeral) or the existing acp test file
- Build artifact: `dist/`

**Interfaces:**
- Produces: a built fork `dist/index.js` whose acp session is ephemeral when `loadEphemeralSession()` is true; CacheFirstLoop accepts `session: string | null`.

- [ ] **Step 1: Write the failing vitest for the ephemeral config + session**

In the fork, add a test (e.g. `src/config.ephemeral.test.ts`) asserting: `loadEphemeralSession()` returns true when its env/config flag is set and false by default; and that `buildSession`-time session resolves to `null` when ephemeral is on. Model it on the existing `config-cache-economics` test style.

```ts
import { describe, it, expect } from "vitest";
import { loadEphemeralSession } from "./config.js";
describe("ephemeral session", () => {
  it("defaults off", () => { delete process.env.REASONIX_ACP_EPHEMERAL_SESSION; expect(loadEphemeralSession()).toBe(false); });
  it("on when env=1", () => { process.env.REASONIX_ACP_EPHEMERAL_SESSION = "1"; expect(loadEphemeralSession()).toBe(true); delete process.env.REASONIX_ACP_EPHEMERAL_SESSION; });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run (in the fork): `npx vitest run src/config.ephemeral.test.ts`
Expected: FAIL — `loadEphemeralSession is not exported` (not implemented yet).

- [ ] **Step 3: Add `loadEphemeralSession()` to `src/config.ts`**

Following the existing `loadKeepalive*` loader style in `src/config.ts`:
```ts
export function loadEphemeralSession(): boolean {
  const v = (process.env.REASONIX_ACP_EPHEMERAL_SESSION ?? "").toLowerCase();
  return v === "1" || v === "true" || v === "yes" || v === "on";
}
```

- [ ] **Step 4: Widen the session type in `src/loop.ts`**

Change `session?: string;` (~line 112 in `CacheFirstLoopOptions`) to `session?: string | null;`. The runtime already handles null (`this.sessionName = opts.session ?? null` at ~240, and the disk paths are null-gated), so this is type-only.

- [ ] **Step 5: Wire the trigger in `src/cli/commands/acp.ts`**

At `buildSession` (~line 194) change `session: \`acp-${timestampSuffix()}\`,` to:
```ts
session: loadEphemeralSession() ? null : `acp-${timestampSuffix()}`,
```
Import `loadEphemeralSession` from `../../config.js`.

- [ ] **Step 6: Run the new test + the full fork suite**

Run: `npx vitest run src/config.ephemeral.test.ts`
Expected: PASS.
Run: `npx vitest run` (the whole fork suite — acp-keepalive, loop-ping-cache-prefix, config-cache-economics, etc.)
Expected: all PASS (the type widen + trigger must not break existing tests).

- [ ] **Step 7: Build the fork**

Run: `npm run build`
Expected: completes; `dist/index.js` exists. Run `node -e "import('./dist/index.js').then(m => console.log(Object.keys(m).filter(k=>/Loop|Client|Prefix/.test(k))))"` and confirm `DeepSeekClient`, `CacheFirstLoop`, `ImmutablePrefix` are exported.

- [ ] **Step 8: Commit (in the fork repo)**

```bash
git add -A
git commit -m "feat(acp): ephemeral session via loadEphemeralSession() + widen session type to string|null"
```

---

### Task 2: Export `buildCodeToolset` + `loadEndpoint` from the fork; rebuild

**Files (FORK):**
- Modify: `src/index.ts` (add exports)
- Build: `dist/`

**Interfaces:**
- Consumes: `buildCodeToolset` (src/code/setup.ts:62), `loadEndpoint` (its definition — find it; likely src/config.ts or src/client.ts).
- Produces: `dist/index.js` re-exporting `buildCodeToolset` and `loadEndpoint` so the fleet shim can build a fully-wired loop.

- [ ] **Step 1: Write the failing export test**

Add `src/index.exports.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import * as lib from "./index.js";
describe("library exports for the fleet", () => {
  it("exports buildCodeToolset", () => expect(typeof lib.buildCodeToolset).toBe("function"));
  it("exports loadEndpoint", () => expect(typeof lib.loadEndpoint).toBe("function"));
  it("exports the engine core", () => { for (const k of ["DeepSeekClient","CacheFirstLoop","ImmutablePrefix"]) expect(lib).toHaveProperty(k); });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run src/index.exports.test.ts`
Expected: FAIL — `buildCodeToolset`/`loadEndpoint` undefined.

- [ ] **Step 3: Locate `loadEndpoint`**

Run: `grep -rn "export.*loadEndpoint\|function loadEndpoint" src/`
Note its source module (so the re-export path is correct).

- [ ] **Step 4: Add the exports to `src/index.ts`**

```ts
export { buildCodeToolset } from "./code/setup.js";
export { loadEndpoint } from "<the module from Step 3>";
```

- [ ] **Step 5: Run the test + rebuild**

Run: `npx vitest run src/index.exports.test.ts`
Expected: PASS.
Run: `npm run build`
Expected: completes.
Run: `node -e "import('./dist/index.js').then(m => console.log('buildCodeToolset' in m, 'loadEndpoint' in m))"`
Expected: `true true`.

- [ ] **Step 6: Commit (fork)**

```bash
git add -A
git commit -m "feat(lib): export buildCodeToolset + loadEndpoint for in-process embedding"
```

---

### Task 3: The Node shim — `engine/run-lane.mjs` in the FLEET repo

**Files (FLEET `/Users/tatlatat/.claude/codex-fleet`):**
- Create: `engine/run-lane.mjs`
- Create: `tests/test-engine-shim.mjs` (a Node test driving the shim with a mocked client)
- Reference: the fork's `dist/index.js`

**Interfaces:**
- Consumes: the fork library (Task 1/2 built dist).
- Produces: a one-shot CLI: reads a JSON request on stdin (`{prompt, system, rootDir, model, maxIterPerTurn}`), runs ONE lane, writes ONE JSON line on stdout: `{text, usage:{prompt_tokens,completion_tokens,prompt_cache_hit_tokens,prompt_cache_miss_tokens}, cost_usd}`; non-zero exit + stderr on error. The gateway's `run_reasonix_acp` (Task 4) will spawn this exactly as it spawned `reasonix acp`.

- [ ] **Step 1: Write the failing shim test (mocked client, no real DeepSeek)**

`tests/test-engine-shim.mjs`: spawn `node engine/run-lane.mjs` with `REASONIX_ENGINE_MOCK=1` (the shim, when this is set, skips the real DeepSeekClient and returns a deterministic `{text:"mock", usage:{...}, cost_usd:0}` — so the test verifies the I/O contract WITHOUT DeepSeek). Feed a request on stdin; assert the stdout JSON has `text`, `usage.prompt_tokens`, `cost_usd`.

```js
import { spawnSync } from "node:child_process";
const req = JSON.stringify({ prompt: "say hi", system: "you are a worker", rootDir: process.cwd(), model: "deepseek-v4-flash", maxIterPerTurn: 1 });
const r = spawnSync("node", ["engine/run-lane.mjs"], { input: req, env: { ...process.env, REASONIX_ENGINE_MOCK: "1" }, encoding: "utf8" });
if (r.status !== 0) { console.error("FAIL: shim exit", r.status, r.stderr); process.exit(1); }
const out = JSON.parse(r.stdout.trim().split("\n").pop());
for (const k of ["text", "usage", "cost_usd"]) if (!(k in out)) { console.error("FAIL: missing", k, out); process.exit(1); }
for (const k of ["prompt_tokens","completion_tokens","prompt_cache_hit_tokens","prompt_cache_miss_tokens"]) if (!(k in out.usage)) { console.error("FAIL: usage missing", k); process.exit(1); }
console.log("PASS: engine shim I/O contract");
```

- [ ] **Step 2: Run to verify it fails**

Run: `node tests/test-engine-shim.mjs`
Expected: FAIL — `Cannot find module engine/run-lane.mjs`.

- [ ] **Step 3: Write `engine/run-lane.mjs`**

The shim (ESM). Resolve the fork dist via `REASONIX_ENGINE_DIST` env (set by install.sh to the built fork's `dist/index.js`), falling back to a sibling `vendor/reasonix-engine/dist/index.js`. With `REASONIX_ENGINE_MOCK=1`, short-circuit to a deterministic reply (for tests/CI). Otherwise:
```js
import fs from "node:fs";
function readStdin(){ return fs.readFileSync(0, "utf8"); }
const req = JSON.parse(readStdin());
if (process.env.REASONIX_ENGINE_MOCK === "1") {
  const text = `mock reasonix lane for ${String(req.prompt).slice(0,40)}`;
  process.stdout.write(JSON.stringify({ text, usage:{prompt_tokens:1,completion_tokens:1,prompt_cache_hit_tokens:0,prompt_cache_miss_tokens:1,cache_hit_ratio:0}, cost_usd:0 }) + "\n");
  process.exit(0);
}
const dist = process.env.REASONIX_ENGINE_DIST || new URL("../vendor/reasonix-engine/dist/index.js", import.meta.url).href;
const { DeepSeekClient, ImmutablePrefix, CacheFirstLoop, buildCodeToolset } = await import(dist);
const toolset = await buildCodeToolset({ rootDir: req.rootDir });
const client = new DeepSeekClient({ apiKey: process.env.DEEPSEEK_API_KEY, baseUrl: process.env.DEEPSEEK_BASE_URL });
const prefix = new ImmutablePrefix({ system: req.system, toolSpecs: toolset.tools.specs() });
const loop = new CacheFirstLoop({ client, prefix, model: req.model, stream: true, session: undefined, maxIterPerTurn: req.maxIterPerTurn ?? 1, tools: toolset.tools });
let text = "", stats = null;
for await (const ev of loop.step(req.prompt)) {
  if (ev.role === "assistant_final") { text = ev.content; stats = ev.stats; }
  if (ev.role === "done") break;
  if (ev.role === "error") { process.stderr.write(String(ev.content || "engine error")); process.exit(3); }
}
const u = stats?.usage ?? {};
process.stdout.write(JSON.stringify({
  text,
  usage: { prompt_tokens: u.promptTokens ?? 0, completion_tokens: u.completionTokens ?? 0,
           prompt_cache_hit_tokens: u.promptCacheHitTokens ?? 0, prompt_cache_miss_tokens: u.promptCacheMissTokens ?? 0,
           cache_hit_ratio: u.cacheHitRatio ?? 0 },
  cost_usd: stats?.cost ?? 0,
}) + "\n");
```
(Field names `promptTokens` etc. are confirmed from the fork's `TurnStats.usage`. Adjust ONLY if Task 1's build shows different casing — verify against `dist/index.js` types.)

- [ ] **Step 4: Run the shim test (mock path)**

Run: `node tests/test-engine-shim.mjs`
Expected: `PASS: engine shim I/O contract`.

- [ ] **Step 5: Commit (fleet)**

```bash
git add engine/run-lane.mjs tests/test-engine-shim.mjs
git commit -m "feat(engine): one-shot Node shim run-lane.mjs (mock-tested I/O contract)"
```

---

### Task 4: Cut the gateway + MCP over to the shim (lockstep) — keep stream/watchdog

**Files (FLEET):**
- Modify: `reasonix-native-gateway.py` (`run_reasonix_acp`: spawn `node engine/run-lane.mjs` instead of `reasonix acp`; map output)
- Modify: `reasonix-fleet-mcp.py` (the dispatch at ~95-136, if it calls the engine independently)
- Modify: `hooks/workflow_selfheal.py` (the reasonix-present check → engine/node presence)
- Test: `tests/test-reasonix-acp.py`, `tests/test-mcp-reasonix.py`, `tests/test-reasonix-fleet.sh`, the gateway HTTP tests

**Interfaces:**
- Consumes: `engine/run-lane.mjs` (Task 3).
- Produces: a gateway whose `run_reasonix_acp` builds the JSON request, spawns the shim (with `REASONIX_ENGINE_MOCK` honored in tests, real otherwise), reads the one-line JSON, and returns the same `(text, usage)` tuple shape the rest of the gateway already consumes. The streaming heartbeat path is preserved (the gateway still streams to its client while the shim runs).

- [ ] **Step 1: Read the current `run_reasonix_acp` contract precisely**

Run: `grep -n "def run_reasonix_acp\|return text\|usage\[\|_read_transcript_cost\|REASONIX_ACP_EPHEMERAL\|reasonix_bin\|command = \[" reasonix-native-gateway.py`
Capture: the exact return shape (`(text, usage_dict)`), which usage keys downstream reads, and the MOCK env it honors (`CLAUDE_REASONIX_GATEWAY_MOCK` / `MOCK_REASONIX_TEXT`).

- [ ] **Step 2: Adjust the existing tests to the shim (TDD)**

`tests/test-reasonix-acp.py` and `tests/test-mcp-reasonix.py` drive the engine path with a fake. Change them to expect the shim: set `REASONIX_ENGINE_MOCK=1` (or the gateway's existing MOCK) so `run_reasonix_acp` returns the mock lane's text/usage via the shim. The assertions stay (a real-ish text + non-zero usage), only the engine handle changes.

- [ ] **Step 3: Run to verify they fail**

Run: `python3 tests/test-reasonix-acp.py`
Expected: FAIL (the gateway still spawns `reasonix acp`, not the shim) — confirms the test now drives the cutover.

- [ ] **Step 4: Rewrite `run_reasonix_acp` to spawn the shim**

Replace the `[reasonix_bin, "acp", ...]` command + the JSON-RPC stdin/stdout + transcript-file polling with: build a request dict `{prompt, system, rootDir, model, maxIterPerTurn}`, `subprocess.Popen(["node", "<INSTALL_HOME>/engine/run-lane.mjs"], stdin=PIPE, stdout=PIPE, stderr=PIPE, env=...)`, write the JSON request, read one JSON line, parse `{text, usage, cost_usd}`, and return `(text, usage_dict)` where usage_dict carries the keys the gateway already reads (`prompt_tokens`, `prompt_cache_hit_tokens`, etc.). Keep honoring the gateway's MOCK env by passing `REASONIX_ENGINE_MOCK=1` to the shim when MOCK is set (so existing mock-mode tests stay green). Keep `session: undefined` semantics by NOT setting any session field. Preserve the streaming heartbeat: the gateway's lazy SSE path that wraps `run_reasonix_acp` is unchanged — the shim is just the producer.

- [ ] **Step 5: Update the MCP + self-heal in lockstep**

`reasonix-fleet-mcp.py`: if its dispatch builds its own engine call, point it at the same shim (or, preferably, route it through the gateway's `run_reasonix_acp` import it already uses). `hooks/workflow_selfheal.py`: change the "reasonix CLI present" preflight check to "node + the engine dist present".

- [ ] **Step 6: Run the engine/MCP tests**

Run: `python3 tests/test-reasonix-acp.py && python3 tests/test-mcp-reasonix.py`
Expected: both PASS via the shim mock path.

- [ ] **Step 7: Integration test + guard + full suite**

Run: `pkill -f reasonix-native-gateway.py; bash tests/test-reasonix-fleet.sh`
Expected: exit 0 (the gateway HTTP blocks now exercise the shim in mock mode).
Run: `python3 tests/test-no-codex-leftovers.py`
Expected: `PASS: no codex leftovers`.
Run: `for t in tests/test-*.py; do python3 "$t" >/dev/null 2>&1 || echo "FAIL $t"; done`
Expected: no FAIL.

- [ ] **Step 8: Commit (fleet)**

```bash
git add -A
git commit -m "refactor(engine): gateway+MCP call the in-process fork shim instead of spawning upstream reasonix acp"
```

---

### Task 5: Retire the dist patch + upstream dependency; bundle the fork; rewrite install/README

**Files (FLEET):**
- Delete: `patches/apply_ephemeral.py`, `patches/ephemeral-session.md`
- Modify: `install.sh` (build/bundle the fork; drop the upstream-reasonix check + the apply_ephemeral step; require node + DEEPSEEK_API_KEY)
- Modify: `bin/claude-reasonix` (drop REASONIX_ACP_EPHEMERAL_SESSION export + any `reasonix` binary resolution that's now unused; set `REASONIX_ENGINE_DIST` / `DEEPSEEK_API_KEY` for the gateway)
- Modify: `README.md` (the engine-is-the-fork story; new requirements)
- Decide: bundle strategy (vendored submodule + build-on-install, OR prebuilt dist committed under `vendor/reasonix-engine/`)

**Interfaces:**
- Consumes: the built fork (Tasks 1-2), the shim (Task 3), the cutover gateway (Task 4).
- Produces: an install that needs node + DEEPSEEK_API_KEY (NO upstream reasonix), with the fork engine bundled, and docs that tell the true story.

- [ ] **Step 1: OWNER GATE — bundle strategy + provenance**

This task has an owner decision (do NOT guess): (a) bundle = git submodule of `Tatlatat/DeepSeek-Reasonix` built on install, OR a prebuilt `dist/` committed under `vendor/reasonix-engine/` in the fleet repo (simplest for end users, larger repo). (b) Fork provenance: reparent the fork's git remote to `github.com/Tatlatat/DeepSeek-Reasonix` + update package.json author/name BEFORE bundling, so the shipped engine is truthfully the owner's. STOP and get the owner's choice before proceeding.

- [ ] **Step 2: Write the failing install assertion**

Add to `tests/test-reasonix-fleet.sh` a static check: the launcher must NOT reference `apply_ephemeral` or check for upstream `reasonix` as a required CLI; the engine dist must be resolvable.
```bash
grep -q "apply_ephemeral" "$LAUNCHER" && fail "launcher must not run the retired dist patch"
grep -q "npm i -g reasonix\|npm install -g reasonix" "$ROOT/install.sh" && fail "install must not require upstream reasonix"
```

- [ ] **Step 3: Run to verify it fails**

Run: `bash tests/test-reasonix-fleet.sh`
Expected: FAIL (apply_ephemeral / upstream-reasonix references still present).

- [ ] **Step 4: Delete the patch + bundle the fork**

`git rm patches/apply_ephemeral.py patches/ephemeral-session.md`. Per the owner's Step-1 choice, add the fork as a submodule (`git submodule add https://github.com/Tatlatat/DeepSeek-Reasonix vendor/reasonix-engine`) or commit its prebuilt `vendor/reasonix-engine/dist/`.

- [ ] **Step 5: Rewrite `install.sh`**

Remove the `npm i -g reasonix` instruction + the reasonix-CLI check + the `apply_ephemeral.py` step. Add: require `node`; require `DEEPSEEK_API_KEY` (check + instruct); build the bundled fork (`npm ci && npm run build` in `vendor/reasonix-engine` if submodule, or no-op if prebuilt dist); set `REASONIX_ENGINE_DIST` to the built `dist/index.js`. Keep the doctor smoke-check.

- [ ] **Step 6: Update the launcher**

In `bin/claude-reasonix`: remove the `REASONIX_ACP_EPHEMERAL_SESSION` export and any now-dead `REASONIX_BIN`/reasonix-binary resolution; export `REASONIX_ENGINE_DIST` + pass `DEEPSEEK_API_KEY` through to the gateway env.

- [ ] **Step 7: Rewrite the README engine story**

The engine is the owner's fork (built using ideas/support FROM reasonix), called in-process; end users install NO upstream reasonix; new Requirements = node + DEEPSEEK_API_KEY; remove the "reasonix ACP patch" section (retired); describe ephemeral/cache as native fork behavior.

- [ ] **Step 8: Static assertions + guard + suite**

Run: `bash tests/test-reasonix-fleet.sh`
Expected: PASS (the apply_ephemeral/upstream checks now pass).
Run: `python3 tests/test-no-codex-leftovers.py && for t in tests/test-*.py; do python3 "$t" >/dev/null 2>&1 || echo "FAIL $t"; done`
Expected: guard green, no FAIL.

- [ ] **Step 9: THE DECISIVE real-DeepSeek bench through the in-process engine**

Run: `pkill -f reasonix-native-gateway.py; python3 runtime/realworld-bench.py`
Expected: `VERDICT: *** ALL GATES PASS ***` — 0 errored/empty/slow, review cache ≥ robust floor, fan-out ≥ floor. This proves the in-process fork engine preserves the cache economics and the lane contract. If a gate FAILS, STOP — the shim/cutover regressed something (likely a usage-field mismatch zeroing cache, or a stream/heartbeat break).

- [ ] **Step 10: Clean-room install test — NO upstream reasonix present**

Run: clone the fleet to a temp dir; temporarily hide upstream reasonix from PATH (`PATH=/usr/bin:/bin REASONIX_BIN= ...`); run `install.sh`; confirm `✓ launcher doctor passed` and that a `realworld-bench` mini-run (or at least the shim mock + a single real lane) works WITHOUT upstream reasonix on PATH. This is the proof of the owner's core requirement: end users need no upstream reasonix.

- [ ] **Step 11: Commit (fleet)**

```bash
git add -A
git commit -m "feat(engine): bundle the fork engine, retire the dist patch + upstream-reasonix requirement; rewrite install/README"
```

---

## Self-Review

**Spec coverage:** spec 2A (migrate essence) → Task 1; 2A exports → Task 2; 2B (shim + cutover) → Tasks 3-4; 2C (retire patch + bundle + docs) → Task 5. The Library Contract → Task 3's shim. The one-shot-vs-persistent open question → resolved to one-shot in Global Constraints with the evidence (per-lane subprocess today, server-side cache). The provenance + bundle open questions → Task 5 Step 1 owner gate.

**Placeholder scan:** every step has the file, the command, and the expected output. The shim code is given in full. The two fork edits cite the verified line numbers + the runtime-already-handles-null fact.

**Type/symbol consistency:** `LoopEvent {role, content, stats}`, `TurnStats.usage.{promptTokens, completionTokens, promptCacheHitTokens, promptCacheMissTokens, cacheHitRatio}`, `TurnStats.cost`, `buildCodeToolset({rootDir}).tools.specs()` — all from verified fork source. The shim's output keys (`prompt_tokens` etc.) match what the gateway's `run_reasonix_acp` already returns, so Task 4's cutover keeps the downstream contract.

**Cross-repo risk noted:** Tasks 1-2 commit in the FORK repo; Tasks 3-5 in the FLEET repo. The implementer must `cd` to the right repo per task and check `git status` in BOTH. The fork build must complete before Task 3 can run a non-mock lane.
