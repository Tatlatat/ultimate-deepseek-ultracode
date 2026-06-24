# Slim-down + Fork-engine — Design Spec

**Date:** 2026-06-24
**Status:** approved-pending-review
**Scope:** two sub-projects, sequenced. Built and verified independently.

## Problem

The published repo (`ultimate-deepseek-ultracode`) is **hỗn tạp** and tells a false story:

1. **Cruft the owner does not use** ships and clutters: Claude Code Router (CCR), Qwen
   (qwen36 / router-qwen), GPT‑5.4 defaults, and legacy codex‑exec concepts
   (service_tier / web_search / sandbox / approval). A new user can't "tải về chạy
   luôn" — the surface is router/CCR-heavy.
2. **The reasonix story is wrong.** The repo tells users to `npm i -g reasonix`
   (UPSTREAM esengine) and post-mutates upstream's compiled `dist/cli/acp-*.js`. The
   engine is actually the **owner's fork** (built using ideas/support *from* reasonix).
   End users must **not** install upstream reasonix; the fork is the engine.
3. **The gateway spawns the upstream binary** (`reasonix acp`) as a subprocess — so the
   system runs upstream while claiming to be the fork (provenance + correctness risk).

## Goals

- Repo ships ONLY the core flow: Claude as main agent + DeepSeek fan-out lanes. Clone →
  install → run, no CCR/qwen/gpt cruft.
- The engine is the owner's fork, called **in-process as a Node library** (CacheFirstLoop),
  with the **full code toolset** (file/shell/semantic-search), matching current behavior.
- End users never install upstream reasonix; the dist-surgery patch (`apply_ephemeral.py`)
  is retired — ephemeral becomes native fork behavior.
- The fork becomes the single source of truth: the one essence the running patched
  upstream has and the fork lacks (the ephemeral-session trigger) is migrated into the
  fork source.

## Global Constraints (copied verbatim where exact)

- **stream:true is load-bearing.** The gateway's 180s watchdog/heartbeat requires the
  streaming path; a non-stream lane reintroduces the silent-180s-kill bug
  (MEMORY: claude-codex-180s-watchdog-nonstream). Pass `stream:true` explicitly.
- **Ephemeral == session:undefined in-process.** When calling CacheFirstLoop in-process
  there is no acp subprocess reading `REASONIX_ACP_EPHEMERAL_SESSION`; the gateway passes
  `session: undefined` directly (zero disk I/O, no lane history bleed).
- **Transcript/usage field contract** (gateway reads these): the engine must surface
  `cost` (precomputed USD float), and usage `prompt_tokens`, `completion_tokens`,
  `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`, `cache_hit_ratio`. `TurnStats`
  from the fork already carries these (loop.ts).
- **Full code toolset.** acp.ts:171 `buildCodeToolset({ rootDir })` — the engine must
  build the same toolset so lanes keep file/shell/semantic-search; do not silently drop
  to a no-tool lane.
- **No upstream reasonix dependency** anywhere in shipped install/docs/gateway.
- **Phase 0 break-fix is mandatory and must land in the same commit as the CCR delete:**
  `bin/claude-reasonix:877-880` is the `[[ -f $CCR_PROXY_FILE ]]` guard INSIDE
  `run_claude_with_fleet()` (the native startup fn every run/on/task uses); plus the
  `CCR_PROXY_FILE` decl, `install.sh` cp-list, `uninstall.sh` rm-list, and
  `tests/test-reasonix-fleet.sh:10,32` unconditional assert. Removing CCR without these
  first makes every launch / the whole test suite exit 1.
- **Guard stays green:** `tests/test-no-codex-leftovers.py` must still pass.
- **git status discipline:** after every workflow, verify `git status` — design agents
  must not write code (MEMORY: reasonix-empty-in-burst-accepted).

---

## Sub-project 1 — Fleet slim-down (do FIRST; independent of the fork)

Remove CCR, qwen, gpt-5.4, codex-exec from the fleet repo, and fix the reasonix story in
the docs/install — WITHOUT yet touching the engine seam (gateway keeps calling the engine
the same way for now; only the cruft and the narrative change).

### 1A. Phase-0 break-fix + CCR removal
- Delete `ccr-claude-proxy.py`.
- `bin/claude-reasonix`: delete the `[[ -f $CCR_PROXY_FILE ]]` guard (877-880) and the
  `CCR_PROXY_FILE` decl (16); delete `run_claude_with_router()` (929-1048),
  `prepare/cleanup_router_runtime` (148-172), `start/stop_ccr_service` (675-726),
  `start/stop_ccr_proxy` (781-843), `ccr_model_list/ccr_alias_model_list` (728-759),
  `router_prompt` (857-862), global CCR_* vars (18-22,28,32-34), CCR env exports (65-66),
  the `router|ccr|router-login|router-qwen` case arms and `generate-ccr-*` arms, and the
  `router/CCR_BIN/CCR_PORT` usage in `usage()`.
- KEEP `report_active_sessions` 'router' string detection (status display only).
- Once `run_claude_with_router` is gone, `generate_ccr_config` (474-612),
  `generate_ccr_agents` (352-460), `with_ccr_tag` (373-374), and the inlined
  custom-router.js heredoc (518-606) are dead → delete. BUT first verify no NATIVE
  consumer reads their output (the agent-type defs live partly here — re-home the
  reasonix-worker/security/reviewer/verify `--agents` definitions to the native
  `generate_agents` path if `generate_ccr_agents` was the only place defining them).
- `doctor()` (1169-1189): strip the CCR_PROXY_FILE / CCR_AGENTS_FILE / CCR_CONFIG_FILE
  references and the `py_compile CCR_PROXY_FILE` so `install.sh` step 5 still passes.
- `install.sh:58` and `uninstall.sh:47`: remove `ccr-claude-proxy.py` from the lists.
- `tests/test-reasonix-fleet.sh`: remove the `$CCR_PROXY` var + unconditional assert
  (10,32), the `CCR_BIN` stub (91), the router/router-login/router-qwen assertion blocks,
  the `generate-ccr-config` block, the router Workflow hook block, the live proxy routing
  block (784-960), and the CCR env assertions (993-994).

### 1B. Qwen removal
- `bin/claude-reasonix`: delete `ensure_qwen36_ready` (761-779), qwen-worker/qwen-research
  agent defs, qwen_prompt + qwen vars, qwen36-local provider + qwenModels JS, router-qwen
  dispatch, all `*_QWEN_*` env.
- `README.md`: remove the router-qwen line.
- `tests/test-reasonix-fleet.sh`: remove the qwen blocks.

### 1C. GPT-5.4 / codex-exec residue
- Replace the stale default string `gpt-5.4` with the real model id (see Global: confirmed
  `deepseek-v4-flash`, already used at reasonix-fleet-mcp.py:108) at
  `bin/claude-reasonix:226,1078` and `reasonix-fleet-mcp.py:261`. Keep the
  `REASONIX_FLEET_MODEL` var NAME (still forwarded + read in status).
- Delete the schema-dead codex-exec fields `service_tier/web_search/sandbox/approval_policy`
  from `reasonix-fleet-mcp.py:210-213,263-266` and their MCP-env forwarding at
  `bin/claude-reasonix:242-245` (run_one_task never forwards them — confirmed dead).
- Delete the dead OpenAI `service_tier` block at `reasonix-native-gateway.py:532-534`
  (no `provider=='openai'` is ever produced; the `raise` at 644/1789 proves only
  `reasonix_cli` is reachable).

### 1D. Hooks + story doc (no engine change yet)
- `hooks/reasonix-workflow.py:254-255` (router/ccr mode aliases) and 448-455 (router
  additionalContext) → remove; the hook then always selects native/fleet.
- `tests/test-workflow-selfheal.py`: change `mode='router'` cases to `mode='native'`.
- README/docs: the "Requirements: install reasonix" and the dist-patch narrative are
  rewritten in Sub-project 2 when the engine actually changes. In Sub-project 1, only
  remove the CCR/qwen/router sections so the docs match the slimmed launcher.

### Sub-project 1 verification
- `tests/test-no-codex-leftovers.py` green.
- `tests/test-reasonix-fleet.sh` exits 0 (slimmed: no router/qwen/CCR blocks).
- Full python suite green.
- `runtime/realworld-bench.py` ALL GATES PASS (engine seam unchanged → real DeepSeek
  lanes still route; this proves the cruft removal didn't break native/fleet).

---

## Sub-project 2 — Fork engine migration + in-process library seam (do SECOND)

### 2A. Migrate the essence into the fork (the 2-line + config-loader change)
In `/Users/tatlatat/Documents/reasonix-fork`:
- `src/loop.ts:112` — widen `session?: string;` → `session?: string | null;` (type-only;
  runtime already handles null at :240 `?? null` and the null-gated disk paths).
- `src/config.ts` — add `loadEphemeralSession(): boolean` in the style of the existing
  `loadKeepalive*` loaders (reads its env/config flag; EPHEMERAL only, NOT UNIQUE).
- `src/cli/commands/acp.ts:194` — replace `session: \`acp-${timestampSuffix()}\`,` with
  `session: loadEphemeralSession() ? null : \`acp-${timestampSuffix()}\`,`.
- `src/index.ts` — add exports the fleet needs for FULL toolset in-process:
  `buildCodeToolset` (from src/code/setup.ts:62) and `loadEndpoint` (and a small
  `buildSession`-style helper if cleaner) so the gateway can construct a fully-wired loop.
- Provenance (the fork is the future source of truth): reparent git remote to
  `github.com/Tatlatat/DeepSeek-Reasonix`, update `package.json` author/repository/name.
  **(Owner-confirmed direction; the exact name is the owner's call.)**
- Build: `npm install && npm run build` so `dist/index.js` exists; run the fork's own test
  suite (acp-keepalive, loop-ping-cache-prefix, config-cache-economics, the new ephemeral
  test).

### 2B. Replace the subprocess with the in-process library in the fleet
- `reasonix-native-gateway.py` `run_reasonix_acp` (1240-…): replace the
  `subprocess.Popen([reasonix_bin, "acp", …])` + JSON-RPC stdin/stdout + transcript-file
  polling with a Node-side call into the fork's `dist/index.js`. Concretely a thin Node
  shim (shipped in the repo, e.g. `engine/run-lane.mjs`) that the gateway invokes (or a
  long-lived Node engine process) implementing the **Library Contract** below; the gateway
  feeds it the prompt + system + toolset rootDir and reads back `{text, usage, cost}` JSON.
  (Decision point at build time: one-shot `node run-lane.mjs` per lane vs a persistent
  Node engine the gateway talks to over a pipe — pick the one that preserves cache warmth
  and the streaming/heartbeat path; persistent is preferred for multi-turn cache.)
- Pass `session: undefined` (ephemeral, in-process), `stream: true`, `maxIterPerTurn`
  appropriate to the lane (1 for prompt→answer, higher for tool loops), `model`
  = the real DeepSeek id, and `buildCodeToolset({ rootDir })` for the full toolset.
- Point `DeepSeekClient` `baseUrl` at the gateway's own proxy via `DEEPSEEK_BASE_URL` (not
  api.deepseek.com directly) so the gateway keeps its observability seam.
- Update `reasonix-fleet-mcp.py:95-136` and `hooks/workflow_selfheal.py:124-132`
  (reasonix-present check) to the new engine handle in lockstep.

### 2C. Retire the dist patch + upstream dependency
- Delete `patches/apply_ephemeral.py` + `patches/ephemeral-session.md`; remove the
  `apply_ephemeral` step from `install.sh` and the launcher.
- `install.sh`: stop checking/instructing `npm i -g reasonix`. Instead build/bundle the
  fork (vendored submodule or prebuilt dist shipped in the repo) and require only
  `DEEPSEEK_API_KEY` for the engine. Node remains a requirement (the engine is Node).
- README/docs rewritten: the engine is the owner's fork (inspired by reasonix), in-process,
  no upstream reasonix install; new requirements (node + DEEPSEEK_API_KEY); the
  ephemeral/cache story told as native fork behavior.

### Library Contract (the seam 2B must satisfy)
```
import { DeepSeekClient, ImmutablePrefix, CacheFirstLoop, buildCodeToolset } from '<fork>/dist/index.js'
const toolset = await buildCodeToolset({ rootDir })
const client  = new DeepSeekClient({ apiKey: DEEPSEEK_API_KEY, baseUrl: DEEPSEEK_BASE_URL })
const prefix  = new ImmutablePrefix({ system, toolSpecs: toolset.tools.specs() })
const loop    = new CacheFirstLoop({ client, prefix, model, stream: true,
                                     session: undefined, maxIterPerTurn, tools: toolset.tools })
for await (const ev of loop.step(text)) {
  if (ev.role==='assistant_final'){ reply=ev.content; stats=ev.stats }
  if (ev.role==='done') break
  if (ev.role==='error') throw new Error(ev.error ?? ev.errorDetail?.message)
}
return { text: reply,
         usage: { prompt_tokens: stats.usage.promptTokens, completion_tokens: stats.usage.completionTokens,
                  cache_hit_tokens: stats.usage.promptCacheHitTokens, cache_miss_tokens: stats.usage.promptCacheMissTokens,
                  cache_hit_ratio: stats.usage.cacheHitRatio },
         cost_usd: stats.cost }
// Reuse ONE loop instance across turns for in-memory cache-warm multi-turn.
```

### Sub-project 2 verification
- Fork builds; fork test suite green.
- `runtime/realworld-bench.py` ALL GATES PASS on real DeepSeek through the in-process fork
  engine — review cache ≥ the robust floor, fan-out ≥ floor, 0 errored/empty/slow. This is
  the decisive proof the library seam preserves the cache economics.
- Clean-clone from GitHub + install → a working engine with NO upstream reasonix present
  (verify by removing/hiding the upstream npm install during the test).

## Top risks (carry into the plan)
1. The engine choke-point `run_reasonix_acp` is called by every lane + keepalive + the MCP
   path — replace in lockstep, never half-migrate.
2. Cost/cache accounting hangs off the transcript fields — verify `TurnStats` maps
   field-for-field or cache% goes dark silently.
3. Ephemeral must reproduce session:undefined or fan-out cache regresses hard + silently.
4. `generate_ccr_agents` partly defines the native agent types — don't delete those
   defs when removing CCR; re-home them to `generate_agents`.
5. Building/bundling the fork (no dist yet) must be reliable across machines (node version,
   npm build) or the one-command install breaks.

## Open questions still owned by the user (deferred to the relevant sub-project)
- Fork rename/name + reparent timing (2A) — direction confirmed, exact name TBD.
- One-shot Node shim vs persistent engine process (2B) — pick for cache warmth.
- Bundle strategy: vendored submodule + build-on-install vs prebuilt dist committed (2C).
