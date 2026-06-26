# Plan: wire the weak-executor harness into the MCP fan-out path (Gap 1 + Gap 2)

**Date:** 2026-06-26
**Branch:** feat/weak-executor-harness (continues the harness work)
**Why:** The e2e validation proved the harness components are correct and the orchestrator
behaves right (Opus 0 direct edits, lanes do the work, cost ~$11.85 partial vs $42), BUT the
harness NEVER ENGAGES on the path reasonix actually fans out through: `mcp__reasonix_fleet__run_reasonix_worker`
calls `run_reasonix_acp(prompt, config)` with `harness=None`. The harness is wired only into the
gateway HTTP callers (/v1/messages, /v1/chat/completions), which the MCP path bypasses. See
[[reasonix-harness-validation-findings]].

## Global constraints (binding)
- DEFAULT OFF + BYTE-INERT: when CLAUDE_REASONIX_GATEWAY_LANE_HARNESS is unset, the MCP worker must
  call run_reasonix_acp EXACTLY as today (harness=None) — byte-identical. The harness only engages
  when the flag is on AND the lane prompt carries an `ACCEPTANCE_TEST:` line.
- The harness must stay CHEAPER: maxAttempts + budgetUsd bound a lane; do not remove those caps.
- No vendored-bundle edit. Reuse the gateway's existing helpers (_lane_harness_on,
  lane_acceptance_test, env_float, env_int) via the already-imported gateway module — do NOT
  duplicate them in the MCP.
- env_truthy/env_int/env_float footgun: pass `default=` as a KEYWORD.
- e2e not unit: the test must drive the REAL MCP run_one_task (with REASONIX_ENGINE_MOCK so no
  DeepSeek) and assert the harness dict reaches run_reasonix_acp.

## Task 1 — Gap 1: wire the harness into the MCP worker (run_one_task)
**File:** `reasonix-fleet-mcp.py` (run_one_task, ~line 116-127). **Test:** `tests/test-mcp-harness-wiring.py`

In run_one_task, BEFORE the `rx(prompt, config)` call, build a harness dict when the harness is
on and the prompt has an acceptance test — reusing the gateway module's helpers:
```python
    gw = _reasonix_gateway_module()
    _harness = None
    if gw is not None and getattr(gw, "_lane_harness_on", None) and gw._lane_harness_on():
        _at = gw.lane_acceptance_test(prompt) if hasattr(gw, "lane_acceptance_test") else ""
        if _at:
            _harness = {
                "acceptanceTest": _at,
                "budgetUsd": gw.env_float("CLAUDE_REASONIX_GATEWAY_LANE_BUDGET_USD",
                                          "CLAUDE_CODEX_GATEWAY_LANE_BUDGET_USD", default=0.05),
                "harnessMaxAttempts": gw.env_int("CLAUDE_REASONIX_GATEWAY_LANE_MAX_ATTEMPTS",
                                                 "CLAUDE_CODEX_GATEWAY_LANE_MAX_ATTEMPTS", default=4),
            }
```
NOTE: `lane_acceptance_test` takes `messages` (a list) in the gateway and runs `lane_task_text` on
it. The MCP has a raw `prompt` STRING, not messages. Two options — pick the one that works without
changing the gateway: (a) call `gw.lane_acceptance_test([{"role":"user","content":prompt}])` so it
goes through lane_task_text; OR (b) if lane_acceptance_test can't take that shape, scan the prompt
string directly for an `ACCEPTANCE_TEST:` line in the MCP (small inline helper). VERIFY which works
by reading the gateway's lane_acceptance_test + lane_task_text; prefer (a) (reuse), fall back to (b).

RESOLVED by controller (verified at runtime): option (a) WORKS — `gw.lane_acceptance_test([{"role":"user","content":prompt}])` returns the ACCEPTANCE_TEST value from a raw prompt string. Use (a); do NOT add a duplicate inline scanner.

Then pass harness through run_in_executor. `run_in_executor(None, rx, prompt, config)` passes
positional args; to add `harness=`, wrap with functools.partial:
```python
    from functools import partial   # (top-of-file import)
    text, usage = await loop.run_in_executor(None, partial(rx, prompt, config, harness=_harness))
```
BYTE-INERT: when _harness is None (flag off or no acceptance line), partial(rx, prompt, config,
harness=None) is behaviorally identical to rx(prompt, config) (harness defaults None). Confirm.

**Test (e2e, real run_one_task, mock engine):** set REASONIX_ENGINE_MOCK=1 +
CLAUDE_REASONIX_GATEWAY_LANE_HARNESS=1; call run_one_task with a prompt containing
`ACCEPTANCE_TEST: true`; assert the returned text starts with `__HARNESS__:` (proving the harness
engaged through the MCP). And a control: flag OFF → text is the plain mock reply (no __HARNESS__).
Also assert flag ON but NO acceptance line → plain reply (harness needs the line).

## Task 2 — Gap 2: ensure cost logs + a slow lane is bounded/escalated, not hung
**File:** `reasonix-fleet-mcp.py` (the except/return + cost block). **Test:** extend Task 1's test.

Two concrete fixes:
1. The MCP's `except Exception` branch (line ~119) returns `ok:False` WITHOUT logging cost. When
   run_reasonix_acp raises (e.g. timeout with the A3 marker OFF), the lane's partial cost is lost.
   Move/duplicate a best-effort `append_reasonix_cost` into a path that runs even on the error
   return, OR (simpler) rely on the harness (Task 1) bounding the lane so it returns NORMALLY with a
   `__HARNESS__:stagnated/exhausted` result (which flows to the existing cost block) instead of
   raising. PREFER the harness-bounds-it path: with Task 1 done, a slow lane hits maxAttempts/budget
   and returns a structured result under the 600s gateway timeout — verify the harness's worst-case
   wall-clock (maxAttempts × (attempt + 120s test)) is < the 600s CLAUDE_REASONIX_GATEWAY_TIMEOUT,
   and if not, document the relationship + recommend a per-attempt cap or a lower maxAttempts.
2. Confirm the harness attempt's acceptance-test execSync timeout (120s in run-lane.mjs) and
   maxAttempts (4) compose to stay under the gateway shim timeout (600s). If 4×(edit+120s) can
   exceed 600s, the validation re-run will still time out. Add a guard: the harness should also
   respect a wall-clock budget. MINIMAL version: lower the default validation maxAttempts to 3 via
   the existing CLAUDE_REASONIX_GATEWAY_LANE_MAX_ATTEMPTS env (no code change — set in the launch),
   OR add an optional per-lane deadline to runHarness. Decide in the task; keep it OFF-by-default.

**Test:** assert that with the harness on + `ACCEPTANCE_TEST: false` (always fails), run_one_task
returns a `__HARNESS__:` non-pass result (stagnated/exhausted) AND the cost ledger got a row (cost
logging works on the bounded-failure path). Use a temp ledger via CLAUDE_REASONIX_REASONIX_COST_LEDGER.

## Task 3 — re-run the validation (measurement, not a code task)
After Tasks 1-2 reviewed clean + reinstalled: re-run the bun BunTestController split with the
harness on. Confirm: lanes return `__HARNESS__` results (harness engaged), the 3 modules created,
157 tests green, the ledger captures per-lane cost, and report TRUE total cost (orchestrator jsonl +
lane ledger) vs $42/19M. This is the decisive number that was still pending.

## How we'll know it worked
- Task 1 e2e: run_one_task with the flag on + acceptance line → `__HARNESS__:` reply (today: plain).
- Validation re-run: lane ledger grows (per-lane cost recorded), lanes show retry/bounded behavior,
  157 tests green, total cost « $42, Opus still 0 direct edits.
