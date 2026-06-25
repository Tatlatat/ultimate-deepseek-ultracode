#!/usr/bin/env node
// One-shot lane producer for the claude-reasonix fleet.
//
// Reads ONE JSON request on stdin:
//   {prompt, system, rootDir, model, maxIterPerTurn}
// runs ONE DeepSeek lane through the owner's fork engine (imported as a library
// from REASONIX_ENGINE_DIST), and writes ONE JSON line on stdout:
//   {text, usage:{prompt_tokens, completion_tokens,
//                 prompt_cache_hit_tokens, prompt_cache_miss_tokens,
//                 cache_hit_ratio}, cost_usd}
//
// This is a stateless, per-lane subprocess (NOT a persistent engine): it is
// behaviourally identical to the old `reasonix acp` spawn the gateway used, so
// the gateway's streaming/heartbeat/prime-gate machinery is untouched — this
// shim is only the lane PRODUCER. DeepSeek's cache hits come from the
// server-side prefix cache (same prefix bytes), kept warm by the gateway, not
// from any in-memory state here.
//
// stream:true is load-bearing (the gateway's 180s watchdog needs the engine to
// keep producing). session:undefined => ephemeral, zero disk I/O, no lane
// history bleed.
import fs from "node:fs";
import { createRequire } from "node:module";
import { resolveOutlineThreshold } from "./lane-opts.mjs";

// The vendored fork engine is a tsup `noExternal` bundle that interops with a few
// CJS-only transitive deps (safer-buffer/iconv-lite → `require("buffer")`) via an
// esbuild `__require` shim. That shim resolves to the host `require` when one is
// in scope, else throws "Dynamic require of X is not supported". A `.mjs` ESM host
// has NO ambient `require`, so loading the bundle here would throw at eval time.
// Provide the canonical ESM-loads-a-CJS-bundle bridge: a real `require` on the
// global scope (the bundle reads `typeof require`, which falls through to
// globalThis), pinned to THIS module's URL so node-builtin resolution works.
// This must run BEFORE `await import(dist)` below; module-level statements suffice.
if (typeof globalThis.require !== "function") {
  globalThis.require = createRequire(import.meta.url);
}

function readStdin() {
  return fs.readFileSync(0, "utf8");
}

function fail(msg, code) {
  process.stderr.write(String(msg) + "\n");
  process.exit(code ?? 1);
}

let req;
try {
  req = JSON.parse(readStdin());
} catch (e) {
  fail(`run-lane: invalid JSON request on stdin: ${e.message}`, 2);
}

function envNum(name, fallback) {
  const v = process.env[name];
  if (v === undefined || v === "") return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

// MOCK path: deterministic reply with NO DeepSeek call (tests/CI). Values are
// overridable via env so the gateway-side tests can assert real-ish numbers
// without spawning the real engine.
if (process.env.REASONIX_ENGINE_MOCK === "1") {
  const text =
    process.env.REASONIX_ENGINE_MOCK_TEXT ??
    `mock reasonix lane for ${String(req.prompt ?? "").slice(0, 40)}`;
  const prompt_tokens = envNum("REASONIX_ENGINE_MOCK_PROMPT_TOKENS", 1);
  const completion_tokens = envNum("REASONIX_ENGINE_MOCK_COMPLETION_TOKENS", 1);
  const hit = envNum("REASONIX_ENGINE_MOCK_CACHE_HIT_TOKENS", 0);
  const miss = envNum("REASONIX_ENGINE_MOCK_CACHE_MISS_TOKENS", 1);
  const denom = hit + miss;
  const cache_hit_ratio = denom > 0 ? hit / denom : 0;
  const cost_usd = envNum("REASONIX_ENGINE_MOCK_COST", 0);
  process.stdout.write(
    JSON.stringify({
      text,
      usage: {
        prompt_tokens,
        completion_tokens,
        prompt_cache_hit_tokens: hit,
        prompt_cache_miss_tokens: miss,
        cache_hit_ratio,
      },
      cost_usd,
    }) + "\n",
  );
  process.exit(0);
}

const dist =
  process.env.REASONIX_ENGINE_DIST ||
  new URL("../vendor/reasonix-engine/dist/index.js", import.meta.url).href;

let lib;
try {
  lib = await import(dist);
} catch (e) {
  fail(`run-lane: cannot load engine dist (${dist}): ${e.message}`, 4);
}

const { DeepSeekClient, ImmutablePrefix, CacheFirstLoop, buildCodeToolset, loadEndpoint,
  codeSystemPrompt } = lib;
for (const [name, ref] of Object.entries({
  DeepSeekClient,
  ImmutablePrefix,
  CacheFirstLoop,
  buildCodeToolset,
})) {
  if (typeof ref !== "function") {
    fail(`run-lane: engine dist missing export ${name} (got ${typeof ref})`, 4);
  }
}

// Auth resolution: prefer explicit env (DEEPSEEK_API_KEY/DEEPSEEK_BASE_URL the
// gateway forwards), else fall back to the fork's own config resolution
// (loadEndpoint reads ~/.reasonix/config.json) so the in-process engine
// authenticates EXACTLY like the old `reasonix acp` path did — no separate
// DEEPSEEK_API_KEY required on a logged-in machine.
let endpoint = { apiKey: undefined, baseUrl: undefined };
if (typeof loadEndpoint === "function") {
  try {
    endpoint = loadEndpoint() || endpoint;
  } catch {
    /* fall through to env-only */
  }
}
const apiKey = process.env.DEEPSEEK_API_KEY || endpoint.apiKey;
const baseUrl = process.env.DEEPSEEK_BASE_URL || endpoint.baseUrl;

const rootDir = req.rootDir || process.cwd();

let text = "";
let stats = null;
try {
  // Full code toolset (file/shell/semantic-search) so lanes match the old acp.
  const _outlineThreshold = resolveOutlineThreshold(process.env);
  const toolset = await buildCodeToolset(
    _outlineThreshold !== undefined ? { rootDir, outlineThresholdBytes: _outlineThreshold } : { rootDir });

  // --- TEST-ONLY read trace (Lever E ground truth) --------------------------
  // When REASONIX_READ_TRACE_DIR is set, record every ACTUAL file read the lane
  // performs to a PER-PROCESS sidecar (reads-<pid>.jsonl in that dir), one resolved
  // path per line. Per-process because each fan-out lane is a fresh shim subprocess,
  // so PID is a unique lane id and concurrent lanes never collide. This is the ONLY
  // honest ground truth for E's recall: the shim returns just assistant_final.text,
  // so a lane's intermediate read_file calls never reach the bench's SSE stream, and
  // the model's self-reported `files_read` is invented (the F-trap). We observe the
  // single tool dispatch chokepoint — specs()/prefix/toolSpecs are UNTOUCHED, so the
  // cached prefix is byte-identical and this is inert to cache. Off by default.
  const _readTraceDir = process.env.REASONIX_READ_TRACE_DIR;
  if (_readTraceDir) {
    const { appendFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const _traceFile = join(_readTraceDir, `reads-${process.pid}.jsonl`);
    const _origDispatch = toolset.tools.dispatch.bind(toolset.tools);
    toolset.tools.dispatch = async (name, argumentsRaw, opts = {}) => {
      if (name === "read_file" || name === "read_file_isolated") {
        try {
          const a = typeof argumentsRaw === "string"
            ? (argumentsRaw.trim() ? JSON.parse(argumentsRaw) : {})
            : (argumentsRaw || {});
          const p = a.path ?? a.file ?? a.filename ?? a.target ?? "";
          if (p) appendFileSync(_traceFile, JSON.stringify({ tool: name, path: String(p), prompt: String(req.prompt ?? "").slice(0, 60) }) + "\n");
        } catch { /* tracing must never break a lane */ }
      }
      return _origDispatch(name, argumentsRaw, opts);
    };
  }
  // --------------------------------------------------------------------------

  const client = new DeepSeekClient({ apiKey, baseUrl });
  // Prefix system text: when the caller doesn't supply one, build the SAME code
  // system prompt the old `reasonix acp` path used (acp.ts builds
  // codeSystemPrompt(rootDir, {...})). This is the large, BYTE-IDENTICAL shared
  // block that ImmutablePrefix places first — every fan-out lane shares it, so
  // DeepSeek caches it once and later lanes hit it warm. Leaving system empty (the
  // earlier shim behavior) dropped that shared prefix and cost ~2pts of fan-out
  // cache (measured: fan-out fell from ~91% to ~89%). Per-lane instructions still
  // ride in `prompt` as before.
  let systemText = String(req.system ?? "");
  if (!systemText && typeof codeSystemPrompt === "function") {
    try {
      systemText = codeSystemPrompt(rootDir, {
        hasSemanticSearch: toolset.semantic?.enabled ?? false,
        modelId: req.model,
      });
    } catch {
      /* fall back to empty system if the prompt builder is unavailable */
    }
  }
  const prefix = new ImmutablePrefix({
    system: systemText,
    toolSpecs: toolset.tools.specs(),
  });
  const loop = new CacheFirstLoop({
    client,
    prefix,
    tools: toolset.tools,
    model: req.model,
    stream: true, // load-bearing: gateway watchdog needs a live producer
    session: undefined, // ephemeral, zero disk, no lane history bleed
    maxIterPerTurn: req.maxIterPerTurn ?? 1,
    maxOutputTokens: req.maxOutputTokens ?? undefined,
  });

  for await (const ev of loop.step(String(req.prompt ?? ""))) {
    if (ev.role === "assistant_final") {
      text = ev.content ?? "";
      if (ev.stats) stats = ev.stats;
    } else if (ev.role === "error") {
      fail(`run-lane: engine error: ${ev.content || "unknown"}`, 3);
    } else if (ev.role === "done") {
      break;
    }
  }
} catch (e) {
  fail(`run-lane: lane execution failed: ${e?.stack || e?.message || e}`, 3);
}

// Map the fork's TurnStats.usage (a Usage instance) to the shim's flat JSON
// shape. The gateway (Task 4) re-maps THIS to its internal usage dict.
const u = stats?.usage ?? {};
process.stdout.write(
  JSON.stringify({
    text,
    usage: {
      prompt_tokens: u.promptTokens ?? 0,
      completion_tokens: u.completionTokens ?? 0,
      prompt_cache_hit_tokens: u.promptCacheHitTokens ?? 0,
      prompt_cache_miss_tokens: u.promptCacheMissTokens ?? 0,
      cache_hit_ratio: u.cacheHitRatio ?? 0,
    },
    cost_usd: stats?.cost ?? 0,
  }) + "\n",
);
