# Fleet Slim-Down Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Strip CCR, Qwen, GPT-5.4, and codex-exec cruft from the fleet repo so a fresh clone installs and runs the core flow (Claude main + DeepSeek fan-out) with nothing else, WITHOUT changing how the gateway calls the engine.

**Architecture:** This is Sub-project 1 of the slim-and-fork-engine design. It only removes unused modes/files/fields and re-points the docs; the engine seam (gateway → reasonix) is untouched and re-proven by the real-DeepSeek bench at the end. Sub-project 2 (the in-process fork library engine) is a separate plan that depends on the fork being built.

**Tech Stack:** Bash launcher (`bin/claude-reasonix`), Python gateway/MCP (`reasonix-native-gateway.py`, `reasonix-fleet-mcp.py`), Claude Code hooks, the shell integration test (`tests/test-reasonix-fleet.sh`), the Python test suite, and `runtime/realworld-bench.py`.

## Global Constraints

- **Phase-0 break-fix is mandatory and lands in the SAME task as the CCR delete** (Task 1): `bin/claude-reasonix:877-880` is the `[[ -f $CCR_PROXY_FILE ]]` guard INSIDE `run_claude_with_fleet()` (the native startup fn every `run`/`on`/`task` uses). Removing `ccr-claude-proxy.py` without removing this guard, the `CCR_PROXY_FILE` decl (line 16), the `install.sh` cp-list entry, the `uninstall.sh` rm-list entry, and the unconditional test assert (`tests/test-reasonix-fleet.sh:10,32`) makes every launch / the whole test suite exit 1.
- **Native agent types are safe** — `reasonix-worker/security/reviewer/verify` are defined in BOTH `generate_agents` (native, line 312, used via `agents_json` at line 894) AND `generate_ccr_agents` (line 420). The native path does not depend on `generate_ccr_agents`, so deleting CCR does not remove the native agent defs.
- **The engine seam stays unchanged.** Do NOT touch `run_reasonix_acp`, the `reasonix acp` subprocess, or how the gateway dispatches lanes. The only gateway change here is deleting the provably-dead OpenAI `service_tier` block (gateway:530-534).
- **Real model id is `deepseek-v4-flash`** (already used at `reasonix-fleet-mcp.py:108`). Replace the stale default string `gpt-5.4`; keep the `REASONIX_FLEET_MODEL` var NAME.
- **Guard stays green:** `tests/test-no-codex-leftovers.py` must pass after every task.
- **`bash -n` must pass** on `bin/claude-reasonix` after every launcher edit, and `python3 -c "import ast; ast.parse(...)"` on every edited `.py`.
- **git-status discipline:** after any workflow/subagent step, verify `git status` shows only intended files (design agents must not write code).
- **Keep `report_active_sessions` 'router' string detection** (status display only) — it does not require any CCR code to exist.

---

### Task 1: Remove CCR (proxy, router modes, config gen) + Phase-0 break-fix

**Files:**
- Delete: `ccr-claude-proxy.py`
- Modify: `bin/claude-reasonix` (guard 877-880, decl line 16, CCR functions, case arms, usage text, exports)
- Modify: `install.sh:58` (cp-list), `uninstall.sh:47` (rm-list)
- Modify: `tests/test-reasonix-fleet.sh` (remove $CCR_PROXY var+assert, CCR_BIN stub, router/router-login blocks, generate-ccr-config block, router Workflow hook block, live proxy block, CCR env asserts)
- Test: `tests/test-reasonix-fleet.sh`, `tests/test-no-codex-leftovers.py`

**Interfaces:**
- Consumes: native path `agents_json()` → `generate_agents()` (unchanged).
- Produces: a launcher with NO `run_claude_with_router`, NO CCR globals, NO `router|ccr|router-login` dispatch; `doctor()` no longer references CCR; the integration test no longer asserts CCR.

- [ ] **Step 1: Write the failing test — the launcher must not reference CCR**

Add to `tests/test-reasonix-fleet.sh` near the top (after the `assert_file` block, before the prompt checks), a static assertion block:

```bash
# Slim-down: the launcher must carry NO CCR / router-proxy machinery.
for banned in "ccr-claude-proxy" "run_claude_with_router" "start_ccr_proxy" "generate_ccr_config" "CCR_PROXY_FILE"; do
  grep -q "$banned" "$LAUNCHER" && fail "launcher still references removed CCR symbol: $banned"
done
[[ -f "$ROOT/ccr-claude-proxy.py" ]] && fail "ccr-claude-proxy.py should be deleted"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `bash tests/test-reasonix-fleet.sh`
Expected: FAIL — `launcher still references removed CCR symbol: ...` (CCR is still present).

- [ ] **Step 3: Delete `ccr-claude-proxy.py`**

Run: `git rm ccr-claude-proxy.py`

- [ ] **Step 4: Phase-0 break-fix in `bin/claude-reasonix`**

Delete the guard block (lines ~875-880):
```bash
  [[ -f "$CCR_PROXY_FILE" ]] || {
    echo "missing CCR Claude proxy: $CCR_PROXY_FILE" >&2
    exit 1
  }
```
Delete the `CCR_PROXY_FILE` decl (line 16) and the CCR global state vars (`CCR_HOME`, `CCR_CONFIG_DIR`, `CCR_CONFIG_FILE`, `CCR_CUSTOM_ROUTER_FILE`, `CCR_AGENTS_FILE` at 18-22; `CCR_BIN` at 28; `CCR_SERVICE_PID`, `CCR_PROXY_PID`, `CCR_PROXY_URL`, `ROUTER_SESSION_RUNTIME_DIR`, `ROUTER_PREPARED` at 32-34). Delete the CCR env exports (the `export CLAUDE_*_CCR_*` lines at 65-66).

- [ ] **Step 5: Delete the CCR/router functions**

In `bin/claude-reasonix` delete these whole functions: `prepare_router_runtime` (148), `cleanup_router_runtime` (163), `start_ccr_service` (675), `stop_ccr_service` (718), `ccr_model_list` (728), `ccr_alias_model_list` (746), `start_ccr_proxy` (781), `stop_ccr_proxy` (837), `router_prompt` (857), `run_claude_with_router` (929), `generate_ccr_agents` (352), `ccr_agents_json` (463), `generate_ccr_config` (474). (Qwen-only `ensure_qwen36_ready` is removed in Task 2.)

- [ ] **Step 6: Remove router/ccr case-dispatch arms + usage text**

Delete the case arms: `generate-ccr-agents)` (1246), `generate-ccr-config)` (1250), `router|ccr)` (1276), `router-login|ccr-login|router-subscription)` (between 1276-1284), `router-qwen|ccr-qwen|qwen-router)` (1284) — note router-qwen's body is removed here but the qwen *agents/provider* cleanup is Task 2. Remove the `router`/`router-login`/`router-qwen`/`CCR_BIN`/`CCR_PORT` lines from `usage()`.

- [ ] **Step 7: Strip CCR from `doctor()`**

In `doctor()` (~1166) remove: the `[[ -f "$CCR_PROXY_FILE" ]]` check, `generate_ccr_agents`, `generate_ccr_config`, `python3 -m json.tool "$CCR_AGENTS_FILE"`, `python3 -m json.tool "$CCR_CONFIG_FILE"` lines. Keep `generate_config`, `generate_agents`, the hook checks, and `py_compile "$MCP_SERVER_FILE"`.

- [ ] **Step 8: Update `install.sh` and `uninstall.sh`**

`install.sh:58` — remove `ccr-claude-proxy.py` from the `for item in ...` copy list.
`uninstall.sh:47` — remove `ccr-claude-proxy.py` from the `for item in ...` rm list.

- [ ] **Step 9: Strip CCR blocks from the integration test**

In `tests/test-reasonix-fleet.sh` delete: the `assert_file "$CCR_PROXY"` line and the `CCR_PROXY=...` var (10,32), the `export CCR_BIN='/bin/echo'` stub (91), the router/router-login/router-qwen output-assertion blocks (~393-454), the `generate-ccr-config` validation block (~487-527), the router Workflow hook block (~612-644), the live `ccr-claude-proxy.py` routing block (~784-960), and the `CLAUDE_REASONIX_CCR_ROUTE`/`CLAUDE_REASONIX_CCR_*` asserts (~993-994). Keep the native/fleet, generate-agents, gateway HTTP, acp-driver, cost-ledger, only-reasonix-fleet, and mcp blocks.

- [ ] **Step 10: `bash -n` + run the integration test**

Run: `bash -n bin/claude-reasonix && echo OK`
Expected: OK (no syntax error).
Run: `bash tests/test-reasonix-fleet.sh`
Expected: PASS — the banned-symbol assertion now passes and no removed block runs.

- [ ] **Step 11: Run the guard + python suite**

Run: `python3 tests/test-no-codex-leftovers.py`
Expected: `PASS: no codex leftovers`.
Run: `for t in tests/test-*.py; do python3 "$t" >/dev/null 2>&1 && echo "ok $t" || echo "FAIL $t"; done`
Expected: every line `ok` (any FAIL means a removed CCR symbol was still referenced by a test — fix the reference).

- [ ] **Step 12: Commit**

```bash
git add -A
git commit -m "refactor(slim): remove CCR (proxy, router modes, config gen) + phase-0 break-fix"
```

---

### Task 2: Remove Qwen (router-qwen, qwen agents, qwen provider, QWEN env)

**Files:**
- Modify: `bin/claude-reasonix` (`ensure_qwen36_ready`, qwen agent defs, qwen vars, QWEN env)
- Modify: `README.md` (router-qwen line)
- Test: `tests/test-reasonix-fleet.sh`, `tests/test-no-codex-leftovers.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: a launcher with no qwen symbols; the integration test no longer asserts qwen.

- [ ] **Step 1: Write the failing test — no qwen in the launcher**

Add to the slim-down static block in `tests/test-reasonix-fleet.sh`:
```bash
for banned in "ensure_qwen36_ready" "qwen-worker" "qwen36-local" "router-qwen"; do
  grep -q "$banned" "$LAUNCHER" && fail "launcher still references removed qwen symbol: $banned"
done
```

- [ ] **Step 2: Run to verify it fails**

Run: `bash tests/test-reasonix-fleet.sh`
Expected: FAIL — `launcher still references removed qwen symbol: ensure_qwen36_ready` (qwen still present).

- [ ] **Step 3: Delete qwen from the launcher**

In `bin/claude-reasonix` delete: `ensure_qwen36_ready()` (761), the `qwen-worker`/`qwen-research` agent defs (in `generate_agents` if any remain after Task 1 — check both `generate_agents` and any qwen leftover), the `qwen_prompt`/qwen model/route vars, and every `CLAUDE_*_QWEN_*` / `QWEN_*` env read and export. (The router-qwen case arm and the qwen CCR provider were already removed with CCR in Task 1; this step removes the qwen-specific helpers and agent defs that were NOT CCR-scoped.)

- [ ] **Step 4: Remove the router-qwen line from README**

In `README.md` delete the `claude-reasonix router-qwen` command line and any qwen mention in the Commands / How-it-routes sections.

- [ ] **Step 5: Remove qwen blocks from the integration test**

In `tests/test-reasonix-fleet.sh` delete any remaining qwen assertion lines (qwen agent presence, qwen provider, `QWEN_*` env). Most were removed with the router blocks in Task 1; remove leftovers.

- [ ] **Step 6: `bash -n` + integration test + guard + suite**

Run: `bash -n bin/claude-reasonix && bash tests/test-reasonix-fleet.sh`
Expected: PASS.
Run: `python3 tests/test-no-codex-leftovers.py`
Expected: `PASS: no codex leftovers`.
Run: `for t in tests/test-*.py; do python3 "$t" >/dev/null 2>&1 || echo "FAIL $t"; done`
Expected: no FAIL lines.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(slim): remove Qwen (router-qwen, agents, provider, env)"
```

---

### Task 3: Remove GPT-5.4 default + dead codex-exec fields

**Files:**
- Modify: `bin/claude-reasonix:226` (and any other `gpt-5.4`), `reasonix-fleet-mcp.py:210-213,261,263-266`, `reasonix-native-gateway.py:530-534`
- Test: `tests/test-no-codex-leftovers.py`, a new assertion in `tests/test-reasonix-fleet.sh`

**Interfaces:**
- Consumes: nothing.
- Produces: `REASONIX_FLEET_MODEL` defaults to `deepseek-v4-flash`; no `service_tier/web_search/sandbox/approval_policy` in the MCP tool schema or env forwarding; no dead OpenAI `service_tier` block in the gateway.

- [ ] **Step 1: Write the failing test — no gpt-5.4 default, no codex-exec fields**

Add to `tests/test-reasonix-fleet.sh` (after the mcp.json generation block that already validates `reasonix_fleet`):
```bash
grep -q "gpt-5.4" "$LAUNCHER" && fail "launcher must not default to gpt-5.4"
grep -q "gpt-5.4" "$MCP_SERVER" && fail "MCP must not default to gpt-5.4"
for f in service_tier web_search approval_policy; do
  grep -q "\"$f\"" "$MCP_SERVER" && fail "MCP still carries codex-exec field: $f"
done
```

- [ ] **Step 2: Run to verify it fails**

Run: `bash tests/test-reasonix-fleet.sh`
Expected: FAIL — `launcher must not default to gpt-5.4`.

- [ ] **Step 3: Replace the gpt-5.4 default**

In `bin/claude-reasonix:226` change `os.getenv("REASONIX_FLEET_MODEL", "gpt-5.4")` → `os.getenv("REASONIX_FLEET_MODEL", "deepseek-v4-flash")`. Do the same at `reasonix-fleet-mcp.py:261`. Grep for any other `gpt-5.4` and replace identically.

- [ ] **Step 4: Delete the dead codex-exec fields in the MCP**

In `reasonix-fleet-mcp.py` delete the schema properties `service_tier` (210), `web_search` (211), `sandbox` (212), `approval_policy` (213), and the env-forwarding lines `service_tier` (263), `web_search` (264), `sandbox` (265), `approval_policy` (266). Confirm `run_one_task` (95-150) never reads them (it doesn't — they are dead). Also remove their writers in `bin/claude-reasonix` (the mcp.json env block at ~242-245 that sets `REASONIX_FLEET_SERVICE_TIER/WEB_SEARCH/SANDBOX/APPROVAL`).

- [ ] **Step 5: Delete the dead OpenAI service_tier block in the gateway**

In `reasonix-native-gateway.py` delete lines 530-534 (the `if reasoning and config.get("provider") == "openai":` and the `service_tier = ...` / `if service_tier and config.get("provider") == "openai":` block). No `provider=='openai'` is ever produced (the `raise` at 644/1789 proves only `reasonix_cli` is reachable), so this is dead.

- [ ] **Step 6: ast checks + integration test + guard**

Run: `python3 -c "import ast; ast.parse(open('reasonix-fleet-mcp.py').read()); ast.parse(open('reasonix-native-gateway.py').read())" && echo OK`
Expected: OK.
Run: `bash -n bin/claude-reasonix && bash tests/test-reasonix-fleet.sh`
Expected: PASS.
Run: `python3 tests/test-no-codex-leftovers.py`
Expected: `PASS: no codex leftovers`.

- [ ] **Step 7: Run the gateway-loading python suite (the MCP schema change must not break dispatch)**

Run: `python3 tests/test-mcp-reasonix.py && python3 tests/test-reasonix-acp.py`
Expected: both PASS (the MCP still runs a worker; removing unused schema fields didn't break `run_one_task`).

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor(slim): drop gpt-5.4 default + dead codex-exec fields (service_tier/web_search/sandbox/approval)"
```

---

### Task 4: Remove router/ccr branches from the hooks + self-heal test

**Files:**
- Modify: `hooks/reasonix-workflow.py:254-255,448-455`
- Modify: `tests/test-workflow-selfheal.py` (router → native)
- Test: `tests/test-workflow-selfheal.py`, `tests/test-workflow-prefix-guide.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the Workflow hook always selects native/fleet (no router/ccr mode branch); the self-heal + prefix-guide tests assert native behavior only.

- [ ] **Step 1: Read the current hook mode logic**

Run: `grep -n "router\|ccr\|workflow_mode\|additionalContext" hooks/reasonix-workflow.py | head`
Note the `workflow_mode()` router/ccr aliases (254-255) and the router `additionalContext` branch (448-455). The prefix-guide test (`test-workflow-prefix-guide.py`) also has a `("router", "Claude Code Router routes")` marker case that must go.

- [ ] **Step 2: Write/adjust the failing test**

In `tests/test-workflow-prefix-guide.py`, in `test_guide_appends_in_each_mode`, remove the `("router", "Claude Code Router routes")` tuple from the modes list (leaving `fleet` and `native`). In `tests/test-workflow-selfheal.py`, change every `mode='router'` (or `"router"`) argument to `'native'`.

- [ ] **Step 3: Run to verify it fails**

Run: `python3 tests/test-workflow-prefix-guide.py`
Expected: FAIL (the hook still emits a router branch the test no longer expects, OR the removed tuple still referenced router text) — confirms the test now drives the hook change.

- [ ] **Step 4: Remove the router/ccr branch from the hook**

In `hooks/reasonix-workflow.py`: in `workflow_mode()` (254-255) drop the `router`/`ccr`/`claude-code-router`/`claude_code_router` aliases so the function returns only `native` or `fleet`. Remove the `mode=='router'` `additionalContext` branch (448-455); the hook then emits only the native/fleet context.

- [ ] **Step 5: Run the affected tests**

Run: `python3 tests/test-workflow-prefix-guide.py && python3 tests/test-workflow-selfheal.py`
Expected: both PASS.

- [ ] **Step 6: Full guard + suite + integration test**

Run: `python3 tests/test-no-codex-leftovers.py`
Expected: `PASS: no codex leftovers`.
Run: `for t in tests/test-*.py; do python3 "$t" >/dev/null 2>&1 || echo "FAIL $t"; done`
Expected: no FAIL.
Run: `bash tests/test-reasonix-fleet.sh`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(slim): hooks always select native/fleet (drop router/ccr branch); tests router->native"
```

---

### Task 5: Trim the docs + final real-DeepSeek verification

**Files:**
- Modify: `README.md` (remove CCR/router/qwen sections; keep the reasonix-engine story as-is for now — the engine narrative is rewritten in Sub-project 2)
- Test: `runtime/realworld-bench.py`, the full suite, the integration test

**Interfaces:**
- Consumes: the slimmed launcher/gateway/hooks/MCP from Tasks 1-4.
- Produces: a repo whose docs describe only the core flow, with the engine seam re-proven on real DeepSeek.

- [ ] **Step 1: Trim README to the core flow**

In `README.md` remove the "router" / "Claude Code Router mode" / "router-qwen" paragraphs and the router-mode commands; keep Requirements, Install, Quick start, the core Commands (`on/off/status/workers/task/run/plain/doctor`), the safe-mode How-it-routes paragraph, Defaults (now without qwen/gpt-5.4), the reasonix patch section (unchanged here), Uninstall, and Layout. Do NOT yet rewrite the "engine is the owner's fork / no upstream reasonix" story — that is Sub-project 2 (the engine hasn't changed yet).

- [ ] **Step 2: Verify no removed symbol leaked into docs/tests**

Run: `grep -rnE "codex-gateway|qwen36|router-qwen|run_claude_with_router|ccr-claude-proxy" --include="*.md" --include="*.sh" --include="*.py" . | grep -vE "/docs/superpowers/|/.git/"`
Expected: no output (every removed symbol is gone from shipped files; design specs/plans under docs/superpowers may still mention them historically).

- [ ] **Step 3: Full python suite + guard**

Run: `python3 tests/test-no-codex-leftovers.py && for t in tests/test-*.py; do python3 "$t" >/dev/null 2>&1 || echo "FAIL $t"; done`
Expected: `PASS: no codex leftovers` and no FAIL.

- [ ] **Step 4: Integration test**

Run: `pkill -f reasonix-native-gateway.py; bash tests/test-reasonix-fleet.sh`
Expected: exit 0, all sub-suites PASS.

- [ ] **Step 5: Real-DeepSeek bench (the decisive proof the slim-down didn't break the engine seam)**

Run: `pkill -f reasonix-native-gateway.py; python3 runtime/realworld-bench.py`
Expected: `VERDICT: *** ALL GATES PASS ***` — 0 errored/empty/slow, review cache ≥ robust floor, fan-out ≥ floor. (The gateway still spawns `reasonix acp` as before; this confirms removing CCR/qwen/gpt did not disturb the native/fleet lane path.)

- [ ] **Step 6: Clean-clone install smoke test**

Run:
```bash
T=$(mktemp -d); git clone -q "$PWD" "$T/c"; cd "$T/c"
TH=$(mktemp -d)/rf; TB=$(mktemp -d)/bin
CLAUDE_REASONIX_FLEET_INSTALL_HOME="$TH" CLAUDE_REASONIX_BIN_DIR="$TB" bash install.sh 2>&1 | grep -E "doctor passed|✗"
cd - ; rm -rf "$T" "$(dirname "$TH")" "$(dirname "$TB")"
```
Expected: `✓ launcher doctor passed` (a fresh clone of the slimmed repo still installs cleanly).

- [ ] **Step 7: Commit + push to publish**

```bash
git add -A
git commit -m "docs(slim): trim README to the core flow (no router/qwen)"
git push publish main
```

---

## Self-Review

**Spec coverage:** Sub-project 1 sections 1A (CCR) → Task 1, 1B (qwen) → Task 2, 1C (gpt/codex-exec) → Task 3, 1D (hooks + story) → Tasks 4-5. The Phase-0 break-fix global constraint → Task 1 Steps 3-9. The "re-home native agent defs" risk is resolved by the Global Constraint note (native defs already exist in `generate_agents`), so no re-home task is needed.

**Placeholder scan:** every step has the exact file:line, the exact command, and the expected output. No TBD.

**Type/symbol consistency:** the banned-symbol lists in Task 1/2 tests match the functions deleted in the same task. `REASONIX_FLEET_MODEL` is preserved (only its default value changes). `deepseek-v4-flash` is used consistently as the replacement default.

**Deferred to Sub-project 2 (NOT in this plan):** the in-process fork library engine, retiring `apply_ephemeral.py` / the upstream-reasonix install requirement, and the README engine-story rewrite. Sub-project 1 leaves the engine seam exactly as-is and re-proves it with the bench.
