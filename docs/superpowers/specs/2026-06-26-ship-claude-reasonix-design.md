# Ship claude-reasonix as public OSS — Design

**Date:** 2026-06-26
**Status:** Design (approved in brainstorming section-by-section, pending spec review)
**Goal:** Prepare the claude-reasonix project (~10K lines Python, a DeepSeek-via-reasonix
fleet for Claude Code) for a **public OSS release** — a stranger can clone, install, and use
it without internal knowledge. Highest quality bar: clear docs, solid install, no confusing
experimental flags surfaced, good errors, CI.

## Audience & quality bar
Public OSS — strangers use it. So: refactor for maintainability AND polish for release
(docs, install, CI, version/tag/push). All four shipping areas are in scope.

## Decomposition — 4 sequential sub-projects
This is too large for one spec/plan. Split into 4 sub-projects, each with its own
spec → plan → execute cycle. Order matters: refactor FIRST (so CI/docs reflect the new
structure), push LAST.

| # | Sub-project | Goal | Depends on |
|---|---|---|---|
| **A** | Refactor gateway | Split `reasonix-native-gateway.py` (3214 lines) into modules, byte-identical engine-seam | — (first) |
| **B** | CI + test entry-point | GitHub Actions runs the full test suite + guard on every PR; one `tests/run-all` | after A |
| **C** | Docs + install | README for strangers, ~5 main flags + an `advanced` section (hiding ~66 flags), install/uninstall tested | after A |
| **D** | Version + tag + push | bump version, tag release, push to the public repo (PR into branch-protected main) | last |

**Why split:** each sub-project is independent and separately testable; a problem in one
doesn't block the whole release. Sub-project A is the highest-risk (touches the prefix
cache) so it goes first, in isolation. **This spec details Sub-project A only.** B/C/D get
their own brainstorm + spec when their turn comes.

## Flag policy (decided, applies across the release)
Keep ALL lever code, but HIDE experimental flags: surface only the ~5 measured/promoted
flags (READ_SUMMARY, READER_BROADEN, READ_RETRY_HOLLOW, LANE_FAIL_MARKER, OVERSCOPE_REJECT)
in the main docs; gather the ~66 experimental flags into an `advanced/internal` docs section,
all default OFF + byte-inert. Preserves the work already built. (Implemented in Sub-project C.)

---

# Sub-project A — Refactor `reasonix-native-gateway.py`

## The problem
One 3214-line file, 100 functions, 71 env flags, doing far too much: env-helpers, 6 levers
(C/D/read-cache/prime-gate/keepalive/loop-breaker), the harness, prompt/prefix building (the
engine-seam), the cost ledger, and the HTTP/SSE server. Hard to read, maintain, or contribute to.

## Binding constraints
1. **Byte-identical engine-seam.** Every prompt/prefix/request-building function
   (`openai_messages_to_prompt`, `lane_task_text`, `run_reasonix_acp`, `normalize_prefix`, and
   the helpers they call) must produce BYTE-IDENTICAL output before/after the refactor. A byte
   test proves it. A single changed byte collapses the 96–99% prefix cache.
2. **Behavior unchanged.** This is a pure refactor (move code) — NO logic changes, NO flag/
   default changes. All 51 existing tests pass unchanged + `test-no-codex-leftovers` passes.
3. **realworld-bench meets gates** after the refactor (review ~99%, fan-out ~91%) on real
   DeepSeek — proves the cache did not collapse.

## Module split
Split `reasonix-native-gateway.py` into a package `reasonix_gateway/`, one responsibility each:

| Module | Contains | Risk |
|---|---|---|
| `env.py` | env_truthy/int/float/first (the 4 helpers used everywhere) | low |
| `engine_seam.py` | prompt/prefix building + run_reasonix_acp + normalize_prefix (byte-critical zone) | **HIGH — byte-test** |
| `levers.py` | Lever C/D / read-cache / prime-gate / keepalive / loop-breaker / overscope / read-summary | med |
| `harness.py` | C3 helpers: parse_harness_result, harness_lane_reply, lane_acceptance_test, _clean_acceptance_command, _bulk_scope_match, _lane_harness_on | low |
| `cost.py` | append_reasonix_cost + ledger | low |
| `server.py` | Handler (HTTP/SSE), do_POST/do_GET, send_sse_* | med |
| `__main__.py` | entry-point (arg parse, ThreadingHTTPServer bootstrap) | low |

**Keep `reasonix-native-gateway.py` at its current path as a THIN SHIM** that does
`from reasonix_gateway import *` (re-exporting the public names) — the launcher, MCP,
install.sh, and all 51 tests import from that path. Not changing the public path = nothing
downstream breaks. The shim is the compatibility seam.

## How (mechanical move, TDD per module)
Move code in dependency order, smallest-risk first; run the full suite after each module:
1. `env.py` (no deps) → suite green.
2. `cost.py`, `harness.py` (leaf helpers) → suite green.
3. `levers.py` → suite green.
4. `server.py` → suite green.
5. `engine_seam.py` LAST + most careful — byte-test BEFORE and AFTER the move; suite green;
   realworld-bench gate.
6. Convert `reasonix-native-gateway.py` to the thin re-export shim; full suite + guard + bench.

Each step is a pure move (cut a function, paste into the module, fix imports) — never
"gather-then-rewrite". If a move would require a logic change to work, STOP — that's a
behavior change, out of scope for this refactor.

## New tests
- `test-engine-seam-byte-identical.py`: build a prompt from a FIXED set of messages + tools
  via the assembled path, assert the bytes match a recorded golden (captured from the
  pre-refactor code) — guards the cache-critical output across the move.
- All 51 existing tests are kept and must pass unchanged (they import via the shim path).

## How we'll know it worked
1. All 51 tests + `test-no-codex-leftovers` + the new byte test pass.
2. `realworld-bench.py` meets its gates on real DeepSeek (review ~99%, fan-out ~91%) — cache intact.
3. `reasonix-native-gateway.py` is now a thin shim; the real code lives in `reasonix_gateway/`
   modules, each focused and independently readable.
4. Launcher + MCP + install.sh still work unchanged (they import the same public path).

## Out of scope (this sub-project)
- No logic/behavior/flag changes (pure structural move).
- No docs/README/install/CI/version work — those are Sub-projects B/C/D.
- No engine/run-lane.mjs or vendor/ changes.
- No flag hiding (that's the docs sub-project C).
