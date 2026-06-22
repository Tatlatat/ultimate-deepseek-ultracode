# Strip Codex — Phase 1 (remove code) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all codex-engine and direct-DeepSeek-HTTP code from the codex-fleet infrastructure so the gateway runs a single engine (`reasonix_cli`), without changing reasonix behavior or any names.

**Architecture:** Surgical deletion in `~/.claude/codex-fleet`, guided by the rule: delete code that *invokes a non-reasonix engine*; keep shape-conversion helpers the reasonix path reuses (even those named `openai_*`). The offline `tests/test-reasonix-acp.py` (fake reasonix binary, currently PASS) is the per-step gate after every gateway change.

**Tech Stack:** Python 3 (gateway, MCP, hooks), bash (launcher, tests). No new dependencies.

## Global Constraints

- Spec of record: `docs/superpowers/specs/2026-06-22-strip-codex-phase1-design.md`. Every task implicitly includes its requirements.
- Phase 1 ONLY removes code / prunes branches. It does NOT rename any file, env var (`CLAUDE_CODEX_*`), MCP tool (`run_codex_worker`/`run_codex_fleet`), agentType label (`codex-*`/`deepseek-*`), or the repo — that is Phase 2.
- **KEEP `codex_cli_semaphore`** — it is shared: `run_reasonix_acp` (codex-native-gateway.py:1163) calls it. Deleting it breaks reasonix.
- The reasonix request path must not change behavior. After every gateway step, `python3 tests/test-reasonix-acp.py` must print `PASS`.
- Shape-conversion functions (`anthropic_messages_to_openai`, `openai_messages_to_prompt`, `openai_response_to_anthropic`, `provider_chat_payload`, structured-output helpers) keep their current names and bodies in Phase 1.
- The agentType labels `codex-*`/`deepseek-*` are routing labels that already point to `claude-reasonix-flash` in reasonix flavor — they are NOT codex engine and are KEPT in Phase 1 (the hook comment at `hooks/codex-workflow.py:193-205` documents this).
- After this phase the gateway advertises ONLY `claude-reasonix-flash`; any other model returns `400 unknown model`, never a crash.
- The launcher (`~/.local/bin/claude-codex`) is NOT git-tracked; back it up to `claude-codex.pre-gd1` before editing.
- Each task is one commit; verify before committing so any task can be reverted independently.
- Working directory for all tasks: `~/.claude/codex-fleet` (git-tracked, base commit before Phase 1: run `git rev-parse --short HEAD` first and record it). The launcher lives at `~/.local/bin/claude-codex`.

---

## File Structure

- `codex-native-gateway.py` — MODIFY: registry → reasonix only; delete codex-engine functions; prune codex/HTTP branches in `call_openai_compatible` and `call_openai_chat_completion`; clean dispatch + error strings.
- `codex-fleet-mcp.py` — MODIFY: drop the non-reasonix path in `run_one_task` (already reasonix-aware).
- `hooks/codex-workflow.py` — MODIFY: nothing engine-related to remove if agentType labels stay; only remove dead codex-only helper text if present (verify).
- `hooks/workflow_selfheal.py` — MODIFY: remove the DeepSeek-key→codex-pro remap probe; keep the reasonix/gateway-reachability probe.
- `hooks/only-codex-fleet.py` — MODIFY: keep Agent-blocking; remove codex-only wording (no logic change).
- `~/.local/bin/claude-codex` — MODIFY (untracked): remove codex flavor registry/route/env; keep reasonix flavor + shared infra.
- DELETE whole files: `system-prompt.md`, `tests/e2e-tmux-claude-codex.sh`, `tests/test-e2e-evidence.sh`, `tests/verify-e2e-evidence.py`, `tests/test-ccr-proxy-timeout.py`, `tests/test-ccr-proxy-streaming.py`.
- PRUNE mixed tests: `tests/test-codex-fleet.sh`, `tests/test-gateway-nonstream-heartbeat.py`.
- `README.md` — MODIFY: reasonix-only description.

---

## Task 1: Gateway G1 — registry to reasonix-only

**Files:**
- Modify: `codex-native-gateway.py` — `model_registry()` (def at ~L87; entries `claude-codex-pro` ~L91, `claude-deepseek-pro` ~L100, `claude-reasonix-flash` ~L108)
- Test: `tests/test-reasonix-acp.py` (existing, offline)

**Interfaces:**
- Produces: `model_registry()` returns a dict containing ONLY `claude-reasonix-flash`.

- [ ] **Step 1: Record the base commit**

Run: `cd ~/.claude/codex-fleet && git rev-parse --short HEAD`
Write the SHA down — it is the rollback point and the review-package base for all tasks.

- [ ] **Step 2: Run the safety-net test to confirm green baseline**

Run: `cd ~/.claude/codex-fleet && python3 tests/test-reasonix-acp.py`
Expected: prints `PASS: reasonix acp driver`.

- [ ] **Step 3: Delete the two non-reasonix registry entries**

In `model_registry()`, delete the `"claude-codex-pro": {...}` and `"claude-deepseek-pro": {...}` dict entries entirely. Also delete the now-unused local at the top of the function:
```python
    codex_provider = "openai" if codex_backend in {"openai", "openai-compatible", "api"} else "codex_cli"
```
and any `codex_backend = ...` line it depends on (grep `codex_backend` inside `model_registry`). Keep the `"claude-reasonix-flash": {...}` entry exactly as-is.

- [ ] **Step 4: Run the safety-net test**

Run: `cd ~/.claude/codex-fleet && python3 tests/test-reasonix-acp.py`
Expected: `PASS`. (The reasonix entry and `run_reasonix_acp` are untouched, so it must still pass.)

- [ ] **Step 5: Verify the registry advertises only reasonix**

Run:
```bash
cd ~/.claude/codex-fleet && python3 -c "
import importlib.util; from pathlib import Path
spec=importlib.util.spec_from_file_location('gw','codex-native-gateway.py'); gw=importlib.util.module_from_spec(spec); spec.loader.exec_module(gw)
print(sorted(gw.model_registry().keys()))
"
```
Expected: `['claude-reasonix-flash']`

- [ ] **Step 6: Commit**

```bash
cd ~/.claude/codex-fleet
git add codex-native-gateway.py
git commit -m "refactor(gateway): registry to reasonix-only (drop codex-pro, deepseek-pro)"
```

---

## Task 2: Gateway G2 — delete codex-engine functions

**Files:**
- Modify: `codex-native-gateway.py` — delete functions: `run_codex_cli` (L914-1061), `extract_codex_final_text` (L894-913), `retryable_codex_cli_failure` (L76-86), `mock_openai_chat_response` (L468-493), `last_openai_user_text` (L461-467), `provider_openai_chat_payload` (L494-512), `openai_has_successful_structured_output` (L698-730), `openai_tool_call_response` (L828-867), `openai_stop_response` (L868-893)
- Test: `tests/test-reasonix-acp.py`

**Interfaces:**
- Consumes: registry from Task 1 (reasonix-only).
- Produces: gateway module with no codex-engine functions; `codex_cli_semaphore` REMAINS (reasonix uses it).

> NOTE: this task deletes functions whose ONLY callers are the codex branches that Task 3 removes. Because Task 3 hasn't run yet, deleting them now would leave dangling references in the codex branches. **Do Task 3 BEFORE Task 2, OR do them together.** To keep tasks independently testable, this plan ORDERS Task 3 (prune branches) before Task 2 (delete now-orphaned functions). The task numbers below reflect that: **execute Task 3 first, then Task 2.** (Kept as separate tasks so each has its own review gate; the controller dispatches Task 3 then Task 2.)

- [ ] **Step 1: Confirm Task 3 already removed the codex branches**

Run: `cd ~/.claude/codex-fleet && grep -n 'run_codex_cli\|mock_openai_chat_response\|openai_tool_call_response\|openai_stop_response\|provider_openai_chat_payload\|openai_has_successful_structured_output\|last_openai_user_text\|extract_codex_final_text\|retryable_codex_cli_failure' codex-native-gateway.py`
Expected: matches appear ONLY at each function's `def` line (no remaining callers). If any caller remains, STOP — Task 3 is incomplete.

- [ ] **Step 2: Delete each codex-engine function body**

Delete these complete `def ...:` blocks from `codex-native-gateway.py` (use the function name to locate each; delete from its `def` line to the line before the next `def`):
`run_codex_cli`, `extract_codex_final_text`, `retryable_codex_cli_failure`, `mock_openai_chat_response`, `last_openai_user_text`, `provider_openai_chat_payload`, `openai_has_successful_structured_output`, `openai_tool_call_response`, `openai_stop_response`.
**Do NOT delete `codex_cli_semaphore`** — `run_reasonix_acp` calls it.

- [ ] **Step 3: Verify the module still imports**

Run: `cd ~/.claude/codex-fleet && python3 -c "import importlib.util; spec=importlib.util.spec_from_file_location('gw','codex-native-gateway.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print('import OK')"`
Expected: `import OK` (no NameError — confirms no remaining reference to a deleted function).

- [ ] **Step 4: Run the safety-net test**

Run: `cd ~/.claude/codex-fleet && python3 tests/test-reasonix-acp.py`
Expected: `PASS`.

- [ ] **Step 5: Commit**

```bash
cd ~/.claude/codex-fleet
git add codex-native-gateway.py
git commit -m "refactor(gateway): delete codex-engine functions (keep shared semaphore)"
```

---

## Task 3: Gateway — prune codex/HTTP branches in the two call_* functions

**Execute this BEFORE Task 2.**

**Files:**
- Modify: `codex-native-gateway.py` — `call_openai_compatible` (def L340; codex branch L358-410; reasonix branch L411+) and `call_openai_chat_completion` (def L1398; codex branch L1402-1470; reasonix branch L1471+), plus the trailing HTTP-openai/deepseek path each ends with.
- Test: `tests/test-reasonix-acp.py`

**Interfaces:**
- Consumes: registry from Task 1.
- Produces: both `call_*` functions keep ONLY the reasonix_cli branch + shared head; the codex_cli branch and the api_key/HTTP-openai/deepseek tail are gone. The reasonix branch still calls `anthropic_messages_to_openai`, `openai_messages_to_prompt`, `openai_response_to_anthropic`, `anthropic_end_turn_response`, `run_reasonix_acp`, `append_reasonix_cost` — all KEPT.

- [ ] **Step 1: Read both functions fully before editing**

Run: `cd ~/.claude/codex-fleet && sed -n '340,460p' codex-native-gateway.py` and `sed -n '1398,1540p' codex-native-gateway.py`. Identify, in each: the shared head (before the first `if config.get("provider") ==`), the `codex_cli` branch, the `reasonix_cli` branch (KEEP), and the trailing non-reasonix HTTP path (the `api_key = ...` / `url = ... + "/chat/completions"` / `urllib.request` block that runs when provider is neither codex nor reasonix — DELETE).

- [ ] **Step 2: In `call_openai_compatible`, delete the codex_cli branch and the HTTP tail**

Delete the `if config.get("provider") == "codex_cli":` block (L358-410). Keep the `if config.get("provider") == "reasonix_cli":` block and everything it returns. Delete any trailing `api_key`/`url`/`urllib` HTTP path after the reasonix branch that served the old `deepseek`/`openai` providers (if present in this function). The function should end after the reasonix branch returns, with a final `raise GatewayError(...)` for unknown providers if one is needed (keep the existing final raise if there is one; otherwise the reasonix branch always returns for reasonix_cli configs and unknown providers can't reach here after Task 1).

- [ ] **Step 3: In `call_openai_chat_completion`, delete the codex_cli branch and the HTTP tail**

Delete the `if config.get("provider") == "codex_cli":` block (L1402-1470) and the `mock_openai_chat_response` early-return at the top if it is codex-only (the `if MOCK env` block returning `mock_openai_chat_response` — that mock is codex-shaped; remove it). Keep the `if config.get("provider") == "reasonix_cli":` block (L1471+). Delete the trailing HTTP path that built `provider_openai_chat_payload(...)` and did `urllib.request` for openai/deepseek (the `api_key = str(config.get("api_key") or "")` block onward, ~L1515-end of function), since no provider reaches it after Task 1.

- [ ] **Step 4: Verify module imports and the reasonix branch is intact**

Run: `cd ~/.claude/codex-fleet && python3 -c "import importlib.util; spec=importlib.util.spec_from_file_location('gw','codex-native-gateway.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print('import OK')"`
Expected: `import OK`.
Run: `grep -c 'reasonix_cli' codex-native-gateway.py` — expected ≥ 4 (the reasonix branches survive).

- [ ] **Step 5: Run the safety-net test**

Run: `cd ~/.claude/codex-fleet && python3 tests/test-reasonix-acp.py`
Expected: `PASS`.

- [ ] **Step 6: Commit**

```bash
cd ~/.claude/codex-fleet
git add codex-native-gateway.py
git commit -m "refactor(gateway): prune codex_cli + HTTP branches, keep reasonix path"
```

---

## Task 4: Gateway G4 — dispatch + error strings + unused imports

**Files:**
- Modify: `codex-native-gateway.py` — dispatch checks (`do_POST` ~L1623, L1653: `if provider in ("codex_cli", "reasonix_cli"):`); error messages (~L436, L1522: the "needs an API key... OPENAI_API_KEY for claude-codex-pro or DEEPSEEK_API_KEY..." strings); unused imports.
- Test: `tests/test-reasonix-acp.py`, `tests/test-mcp-reasonix.py`, `tests/test-reasonix-cost-ledger.py`

- [ ] **Step 1: Simplify the dispatch provider checks**

Run `grep -n 'provider in ("codex_cli", "reasonix_cli")' codex-native-gateway.py`. Replace each occurrence with `provider == "reasonix_cli"`.

- [ ] **Step 2: Replace the codex/deepseek error messages**

Run `grep -n 'needs an API key' codex-native-gateway.py`. For each remaining occurrence, replace the message body
`"... needs an API key. Set OPENAI_API_KEY for claude-codex-pro or DEEPSEEK_API_KEY for claude-deepseek-pro before starting claude-codex."`
with
`"only claude-reasonix-flash is served by this gateway."`
(If after Task 3 these messages are already deleted because they lived in removed branches, skip — grep will return nothing.)

- [ ] **Step 3: Remove unused imports**

Run: `cd ~/.claude/codex-fleet && python3 -m pyflakes codex-native-gateway.py 2>/dev/null || python3 -c "import ast,sys; ast.parse(open('codex-native-gateway.py').read()); print('syntax OK')"`
If `pyflakes` is available, remove any import it flags as unused (e.g. `subprocess` if only `run_codex_cli` used it — confirm `run_reasonix_acp` still uses `subprocess` before removing; it does, so `subprocess` STAYS). If pyflakes is unavailable, just confirm syntax parses.

- [ ] **Step 4: Run all three reasonix gateway tests**

Run:
```bash
cd ~/.claude/codex-fleet
python3 tests/test-reasonix-acp.py
python3 tests/test-mcp-reasonix.py
python3 tests/test-reasonix-cost-ledger.py
```
Expected: each prints PASS.

- [ ] **Step 5: Verify no codex-engine code remains**

Run: `cd ~/.claude/codex-fleet && grep -n 'run_codex_cli\|provider == "codex_cli"\|provider == "deepseek"\|mock_openai_chat_response' codex-native-gateway.py`
Expected: no matches (only `codex_cli_semaphore` may appear — that is the kept shared helper).

- [ ] **Step 6: Commit**

```bash
cd ~/.claude/codex-fleet
git add codex-native-gateway.py
git commit -m "refactor(gateway): reasonix-only dispatch + error strings"
```

---

## Task 5: MCP — drop the non-reasonix path in run_one_task

**Files:**
- Modify: `codex-fleet-mcp.py` — `run_one_task` (def ~L133; reasonix branch `if fleet_flavor() == "reasonix":` ~L145; the codex `else`/fallback path after it)
- Test: `tests/test-mcp-reasonix.py`

- [ ] **Step 1: Read run_one_task fully**

Run: `cd ~/.claude/codex-fleet && sed -n '133,230p' codex-fleet-mcp.py`. Identify the reasonix branch (`if fleet_flavor() == "reasonix":`) and the codex path it falls through to (a `codex exec` subprocess call).

- [ ] **Step 2: Make reasonix the only path**

Remove the `if fleet_flavor() == "reasonix":` conditional wrapper and the codex `codex exec` fallback path so the function ALWAYS runs the reasonix acp dispatch (`_reasonix_acp_fn()` + `append_reasonix_cost`). Keep `fleet_flavor()` helper defined (other code or Phase 2 may use it) but the dispatch no longer branches on it. If `_reasonix_acp_fn()` returns None (gateway module not importable), keep the existing error handling that reports that — do not silently fall back to codex (there is no codex anymore).

- [ ] **Step 3: Run the MCP reasonix test**

Run: `cd ~/.claude/codex-fleet && python3 tests/test-mcp-reasonix.py`
Expected: PASS (it uses a fake reasonix acp binary and asserts engine=reasonix + cost captured).

- [ ] **Step 4: Verify no `codex exec` remains in the MCP**

Run: `cd ~/.claude/codex-fleet && grep -n 'codex exec\|run_codex_worker.*codex\|subprocess.*codex' codex-fleet-mcp.py`
Expected: no `codex exec` dispatch (the tool NAME `run_codex_worker` may still appear — that is kept for Phase 2).

- [ ] **Step 5: Commit**

```bash
cd ~/.claude/codex-fleet
git add codex-fleet-mcp.py
git commit -m "refactor(mcp): always dispatch reasonix acp, drop codex exec path"
```

---

## Task 6: Hooks — remove codex/deepseek-key probes, keep reasonix

**Files:**
- Modify: `hooks/workflow_selfheal.py` (DeepSeek-key→codex-pro remap, ~L14-15, `DEEPSEEK_AGENT_TYPES` remap logic), `hooks/only-codex-fleet.py` (codex wording), `hooks/codex-workflow.py` (verify nothing engine-level to remove)
- Test: `tests/test-workflow-selfheal.py`

- [ ] **Step 1: Read the self-heal remap logic**

Run: `cd ~/.claude/codex-fleet && grep -n 'DEEPSEEK_AGENT_TYPES\|claude-codex-pro\|remap\|DEEPSEEK_API_KEY\|codex-cli' hooks/workflow_selfheal.py`. Find the code that, when no `DEEPSEEK_API_KEY` is present, remaps `deepseek-*` agentTypes to `claude-codex-pro`. That remap targets codex, which no longer exists.

- [ ] **Step 2: Remove the codex remap, keep the reachability probe**

In `hooks/workflow_selfheal.py`: delete the "no DEEPSEEK_API_KEY → remap to claude-codex-pro" branch and its helper data (`DEEPSEEK_AGENT_TYPES` remap usage). Keep the gateway/proxy reachability probe and the "restart" guidance, but change any "restart claude-codex" wording to "restart claude-reasonix". Delete the "codex-cli ChatGPT token expired" probe (no codex login anymore).

- [ ] **Step 3: Run the self-heal test**

Run: `cd ~/.claude/codex-fleet && python3 tests/test-workflow-selfheal.py`
Expected: PASS. If the test asserts the codex remap behavior, update the test to assert the reasonix-only behavior (the remap is gone; the reachability probe stays). Show the updated assertions.

- [ ] **Step 4: Clean codex wording in only-codex-fleet.py and codex-workflow.py**

In `hooks/only-codex-fleet.py`: keep the Agent-blocking logic; reword any user-facing string that says "codex" to "fleet"/"reasonix" where it refers to the engine (do NOT rename the file or the `run_codex_worker` tool reference — Phase 2). In `hooks/codex-workflow.py`: the `codex-*`/`deepseek-*` agentType labels STAY (they route to reasonix-flash per the L193-205 comment); only remove genuinely dead codex-engine text if any. Verify with `grep -n 'codex exec\|run_codex_cli' hooks/codex-workflow.py` (expected: none).

- [ ] **Step 5: Commit**

```bash
cd ~/.claude/codex-fleet
git add hooks/workflow_selfheal.py hooks/only-codex-fleet.py hooks/codex-workflow.py tests/test-workflow-selfheal.py
git commit -m "refactor(hooks): drop codex/deepseek-key probes, keep reasonix path"
```

---

## Task 7: Delete codex-only files

**Files:**
- Delete: `system-prompt.md`, `tests/e2e-tmux-claude-codex.sh`, `tests/test-e2e-evidence.sh`, `tests/verify-e2e-evidence.py`, `tests/test-ccr-proxy-timeout.py`, `tests/test-ccr-proxy-streaming.py`

- [ ] **Step 1: Confirm nothing references the files to delete**

Run: `cd ~/.claude/codex-fleet && for f in system-prompt.md e2e-tmux-claude-codex.sh test-e2e-evidence.sh verify-e2e-evidence.py test-ccr-proxy-timeout.py test-ccr-proxy-streaming.py; do echo "== $f =="; grep -rn "$f" . --include='*.py' --include='*.sh' --include='*.md' | grep -v "/$f:"; done`
Also check the launcher: `grep -n 'system-prompt.md' ~/.local/bin/claude-codex` — if the launcher reads `system-prompt.md`, note it; Task 8 will switch it to `system-prompt-reasonix.md`.
Expected: no references except (possibly) the launcher's `system-prompt.md` (handled in Task 8).

- [ ] **Step 2: Delete the files**

```bash
cd ~/.claude/codex-fleet
git rm system-prompt.md tests/e2e-tmux-claude-codex.sh tests/test-e2e-evidence.sh tests/verify-e2e-evidence.py tests/test-ccr-proxy-timeout.py tests/test-ccr-proxy-streaming.py
```

- [ ] **Step 3: Run the reasonix tests to confirm nothing broke**

Run:
```bash
cd ~/.claude/codex-fleet
python3 tests/test-reasonix-acp.py && python3 tests/test-mcp-reasonix.py && python3 tests/test-reasonix-cost-ledger.py && python3 tests/test-workflow-selfheal.py
```
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
cd ~/.claude/codex-fleet
git commit -m "refactor: delete codex-only system prompt and tests"
```

---

## Task 8: Launcher — remove codex flavor (untracked file)

**Files:**
- Modify: `~/.local/bin/claude-codex` (NOT git-tracked — back up first)

- [ ] **Step 1: Back up the launcher**

```bash
cp ~/.local/bin/claude-codex ~/.local/bin/claude-codex.pre-gd1
echo "backup at ~/.local/bin/claude-codex.pre-gd1"
```

- [ ] **Step 2: Read the flavor + codex-config regions**

Run: `grep -n 'CLAUDE_CODEX_FLAVOR\|flavor\|claude-codex-pro\|claude-deepseek-pro\|CODEX_BIN\|CODEX_BACKEND\|OPENAI_API_KEY\|DEEPSEEK_API_KEY\|codex_model\|deepseek_model\|system-prompt.md\|exit 1' ~/.local/bin/claude-codex`. This maps the codex flavor block (registry/route gen at ~L262-263, L353; env at ~L24, L117-119; the codex-flavor guard at ~L36-50).

- [ ] **Step 3: Make reasonix the only flavor**

Edit `~/.local/bin/claude-codex`:
- In the `case "$(basename "$0")"` flavor block, set the flavor to `reasonix` unconditionally (both `claude-codex` and `claude-reasonix` invocations now run reasonix). Remove the `codex` exit-1 guard added earlier — it's moot once codex is gone.
- Remove the codex/deepseek model+route generation: the `codex_model = os.getenv("...", "claude-codex-pro")` / `deepseek_model = os.getenv("...", "claude-deepseek-pro")` lines and the Providers/Router entries that referenced `claude-codex-pro` / `claude-deepseek-pro` / the `anthropic` direct-alias for codex. Keep the reasonix provider + route generation (the `gatewayRoutes` injection from the prior cache fix and the reasonix-flash model entries).
- Remove `CODEX_BIN`, `CODEX_BACKEND`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` env handling that only served codex.
- If the launcher passes `system-prompt.md` to the agent, switch it to `system-prompt-reasonix.md`.

- [ ] **Step 4: Syntax-check the launcher**

Run: `bash -n ~/.local/bin/claude-codex && echo "syntax OK"`
Expected: `syntax OK`.

- [ ] **Step 5: Live smoke — reasonix session still starts and serves**

Run (in a scratch shell):
```bash
claude-reasonix router-login --help >/dev/null 2>&1; echo "launcher runs, exit $?"
```
Then the fuller smoke from the spec: start `claude-reasonix router-login`, wait for the gateway port, and send one request:
```bash
# after the session/gateway is up, find the gateway port and POST:
python3 -c "
import json,urllib.request
body=json.dumps({'model':'claude-reasonix-flash','messages':[{'role':'user','content':'Reply only PONG'}],'max_tokens':30}).encode()
req=urllib.request.Request('http://127.0.0.1:<GATEWAY_PORT>/v1/chat/completions',data=body,headers={'content-type':'application/json'},method='POST')
print(urllib.request.urlopen(req,timeout=60).read().decode()[:200])
"
```
Expected: HTTP 200 with non-empty content (no "unknown model", no codex error). If running a full live session is impractical in the execution environment, at minimum confirm `bash -n` passes and that `grep -c claude-codex-pro ~/.local/bin/claude-codex` is 0; note in the report that the live smoke was deferred.

- [ ] **Step 6: Done (no git commit — launcher is untracked)**

The launcher is not in git. Record in the progress ledger that it was edited and backed up at `~/.local/bin/claude-codex.pre-gd1`.

---

## Task 9: Prune mixed tests + README, push mirror

**Files:**
- Modify: `tests/test-codex-fleet.sh` (prune codex cases), `tests/test-gateway-nonstream-heartbeat.py` (prune codex cases), `README.md`

- [ ] **Step 1: Prune the mixed shell test**

Run: `cd ~/.claude/codex-fleet && grep -n 'codex\|reasonix\|claude-codex-pro\|run_codex_cli' tests/test-codex-fleet.sh`. Remove the test cases that exercise the codex engine (codex exec, claude-codex-pro routing, OPENAI/DEEPSEEK-key paths). Keep cases that exercise the gateway/MCP/reasonix path. If the file becomes entirely reasonix, that's fine; if it becomes empty of meaningful cases, `git rm` it instead and note why.

- [ ] **Step 2: Prune the heartbeat test**

Run: `cd ~/.claude/codex-fleet && grep -n 'codex_cli\|reasonix_cli\|run_codex' tests/test-gateway-nonstream-heartbeat.py`. Remove codex_cli cases; keep reasonix_cli heartbeat cases. Run it: `python3 tests/test-gateway-nonstream-heartbeat.py` → expected PASS (or the reasonix subset passes).

- [ ] **Step 3: Rewrite README codex sections**

In `README.md`, rewrite sections describing the codex flavor/engine to describe the reasonix-only system; change command examples from `claude-codex` to `claude-reasonix`; keep the reasonix content. Remove references to OPENAI/DEEPSEEK_API_KEY backends.

- [ ] **Step 4: Final completeness check**

Run:
```bash
cd ~/.claude/codex-fleet
grep -rn 'run_codex_cli\|provider == "codex_cli"\|provider == "deepseek"\|claude-codex-pro\|claude-deepseek-pro\|codex exec' --include='*.py' --include='*.sh' .
```
Expected: zero engine-code matches. Surviving allowed matches: the kept name `codex_cli_semaphore`, the `CLAUDE_CODEX_*` env names, the `codex-fleet`/`codex_fleet`/`run_codex_worker` names, and historical comments. Eyeball the output to confirm nothing is live codex code.

- [ ] **Step 5: Run the full reasonix test set once more**

Run:
```bash
cd ~/.claude/codex-fleet
python3 tests/test-reasonix-acp.py && python3 tests/test-mcp-reasonix.py && python3 tests/test-reasonix-cost-ledger.py && python3 tests/test-workflow-selfheal.py
```
Expected: all PASS.

- [ ] **Step 6: Commit and push the public mirror**

```bash
cd ~/.claude/codex-fleet
git add tests/test-codex-fleet.sh tests/test-gateway-nonstream-heartbeat.py README.md
git commit -m "refactor: prune codex test cases, reasonix-only README"
git push origin main
```
Expected: push succeeds to `github.com/Tatlatat/claude-codex-fleet`.

---

## Self-Review (completed by plan author)

**Spec coverage:** G1 registry → Task 1 ✅; G2 delete functions → Task 2 ✅; G3 prune branches → Task 3 (ordered before Task 2) ✅; G4 dispatch/errors → Task 4 ✅; MCP → Task 5 ✅; hooks → Task 6 ✅; codex-only file deletion → Task 7 ✅; launcher → Task 8 ✅; mixed tests + README + mirror push → Task 9 ✅; per-step reasonix safety-net gate → in every gateway task ✅; KEEP `codex_cli_semaphore` → Global Constraints + Task 2 Step 2 ✅; keep names (Phase 2 deferral) → Global Constraints + noted in Tasks 5/6/9 ✅.

**Placeholder scan:** none — every step has the exact command and the exact functions/lines. The two steps that must read code first (Task 3 Step 1, Task 8 Step 2) state exactly what to read and why (the call_* branch boundaries and the launcher codex regions must be matched against the real file).

**Ordering note:** Task 3 (prune branches) MUST run before Task 2 (delete now-orphaned functions); both the Task 2 header and the Self-Review state this. The controller dispatches in order: Task 1 → Task 3 → Task 2 → Task 4 → Task 5 → Task 6 → Task 7 → Task 8 → Task 9.

**Type/name consistency:** the kept shared functions (`anthropic_messages_to_openai`, `openai_messages_to_prompt`, `openai_response_to_anthropic`, `append_reasonix_cost`, `run_reasonix_acp`, `codex_cli_semaphore`) are named identically everywhere they appear. The deleted-function list in Task 2 matches the grep-verified boundaries and excludes `codex_cli_semaphore`.
