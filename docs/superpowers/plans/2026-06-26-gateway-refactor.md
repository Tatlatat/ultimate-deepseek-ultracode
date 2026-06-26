# Gateway Refactor (Sub-project A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the 3214-line `reasonix-native-gateway.py` into a focused `reasonix_gateway/` package, keeping a thin shim at the old path and the engine-seam byte-identical.

**Architecture:** Pure mechanical move (cut function + its module-level state → new module → fix imports), in dependency order so imports stay one-directional (env ← leaf-helpers ← levers ← engine_seam ← server). The old file becomes a thin `from reasonix_gateway import *` re-export shim so the launcher, MCP, install.sh, and all 51 tests keep importing the same public path. No logic/behavior/flag changes.

**Tech Stack:** Python 3 stdlib (http.server, threading), no new deps.

## Global Constraints

- **Byte-identical engine-seam.** `openai_messages_to_prompt`, `run_reasonix_acp`, `normalize_prefix`, `lane_task_text`, `anthropic_messages_to_openai`, and every helper they call must produce BYTE-IDENTICAL output before/after. A golden byte test (captured from the pre-refactor code) proves it. One changed byte collapses the 96–99% prefix cache.
- **Behavior unchanged.** Pure structural move — NO logic edits, NO flag/default changes, NO renames of public functions. All 51 existing tests pass UNCHANGED (they import via the shim path). `tests/test-no-codex-leftovers.py` passes.
- **One-directional imports.** Dependency order: `env` ← `text` ← {`harness`, `cost`, `levers`} ← `engine_seam` ← `server`. `engine_seam` MAY import `levers`; `levers` MUST NOT import `engine_seam` (engine-seam calls classify_lane_type / *_directive / read_cache_injection_block / read_lane_summary_instruction / structured_output_prompt_instruction — these all live in `levers`). The shared leaf text-helpers `text_from_content` + `lane_task_text` (called by harness, levers, AND engine_seam) live in the LOW `text.py` module so all three import them DOWNWARD — NO lazy imports needed. If a move would otherwise create a circular import, the function is in the wrong module — move it down a layer, do not add a logic workaround.
- **Move state WITH its owner.** Every module-level global (locks, dicts, compiled regexes, the `JSON` alias) moves into the SAME module as the functions that use it (e.g. `_PRIME_GATES`+`_PRIME_LOCK` → levers; `_READ_SUMMARY_CACHE` → levers; `_PROSE_CUT_RE` → harness). A global referenced from two modules stays in the lower-layer module and is imported.
- **No engine/run-lane.mjs, vendor/, hooks/, launcher, or MCP changes** — gateway-only. The shim keeps the public path stable so none of those need edits.
- **Run from the repo, not the install.** All commands run in `/Users/tatlatat/.claude/codex-fleet`. After the whole refactor, reinstall is a separate manual step (not in this plan).

## Module map (target `reasonix_gateway/` package)

| Module | Functions (by current line) | Module state it owns |
|---|---|---|
| `env.py` | env_first(37) env_int(45) env_float(53) env_truthy(61); the `JSON` alias(32) | JSON |
| `text.py` | text_from_content(812) lane_task_text(1366) json_bytes(808) — the shared leaf text helpers everyone imports downward | (none) |
| `harness.py` | _lane_fail_marker_on(65) lane_unverified_reply(70) _lane_harness_on(90) parse_harness_result(95) harness_lane_reply(111) lane_acceptance_test(123) _clean_acceptance_command(144) | _PROSE_CUT_RE(138) |
| `cost.py` | append_reasonix_cost(2086) summarize_reasonix_cost(2116) weighted_cache(2044) classify_miss(2066) | — |
| `levers.py` | ALL of: preindex(199–328), prime/semaphore(329–365,680–724,763–796), keepalive(394–460), read-cache(461–678), lane-loop-breaker(725–762), classify/synthesis(1361–1442,1581 etc), directives(1443–1620), overscope(1677–1783) — i.e. every lever helper + classify_lane_type + the *_directive/*_instruction builders + read_cache_injection_block | _PRIME_LOCK/_PRIME_GATES, _KEEPALIVE_*, _READ_SUMMARY_CACHE*, _READ_CACHE_LOADED, READ_CACHE_BLOCK_*, _FILE_PATH_RE, _PRIME_SERIAL_*, _LANE_*, _PREINDEX_*, _REASONIX_CLI_SEMAPHORE* |
| `engine_seam.py` | normalize_prefix(839) anthropic_*_to_openai(850–962) openai_response_to_anthropic(963) provider_chat_payload(1015) call_openai_compatible(1038) openai_messages_to_prompt(1784) the structured-output helpers(1892–2010) anthropic_*_response(2011–2043) retry_cap_for_empty(2168) run_reasonix_acp(2191) call_openai_chat_completion(2572) tool_schema_entries/schema_type/fallback_value_from_schema/structured_timeout_fallback/structured_output_prompt_instruction/_schema_has_nested_array_of_objects(1194–1360) estimate_tokens(1011) json_bytes(808) model_registry(797) gateway_trace(329) | (compiled regexes used only here move here) |
| `server.py` | GatewayError(2737) ClientGone(2745) Handler(2751) _keepalive_loop(3160) main(3185) | — |
| `__init__.py` | re-exports the public names (so `from reasonix_gateway import X` works) | — |

**The old `reasonix-native-gateway.py`** becomes: `from reasonix_gateway import *` + an explicit `if __name__ == "__main__": sys.exit(main())`.

NOTE on the map: the boundary between `levers` and `engine_seam` is "does the engine-seam (prompt assembly / run_reasonix_acp) CALL it?" If engine_seam calls it AND it builds prompt text from a lever, it can live in `levers` and be imported by engine_seam (one-directional). `classify_lane_type`, the `*_directive`/`*_instruction` builders, `read_cache_injection_block`, and `overscope_rejection` live in `levers`; `openai_messages_to_prompt` (which calls them) lives in `engine_seam`. The implementer confirms each function's home by where its callers and callees sit — if unsure, the rule is: a function imported by engine_seam but importing nothing from engine_seam goes in levers.

---

### Task 1: Capture the engine-seam golden bytes (the safety net — BEFORE any move)

**Files:**
- Create: `tests/test-engine-seam-byte-identical.py`
- Create: `tests/fixtures/engine_seam_golden.json` (generated by the test on first run, then frozen)

**Interfaces:**
- Consumes: the CURRENT `reasonix-native-gateway.py` (pre-refactor) via importlib.
- Produces: a test that asserts `openai_messages_to_prompt(MSGS, TOOLS)` and `lane_task_text(MSGS)` and `normalize_prefix(SAMPLE)` produce byte-identical output to a frozen golden. Later tasks re-run this after every move.

- [ ] **Step 1: Write the test that captures + compares golden bytes**

```python
#!/usr/bin/env python3
"""Engine-seam byte-identical guard. On first run with REASONIX_FREEZE_GOLDEN=1 it
records the golden output of the cache-critical prompt-building functions; every later
run asserts the CURRENT code produces byte-identical output. This is the net that proves
the refactor did not change a single byte of the assembled prompt (which would collapse
the 96-99% prefix cache)."""
import importlib.util, json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "engine_seam_golden.json"
_spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(gw)

# A FIXED, representative input — a multi-message lane with tools + a system message.
MSGS = [
    {"role": "system", "content": "You are a careful refactoring agent."},
    {"role": "user", "content": "Extract parseX into a module.\nACCEPTANCE_TEST: bun test x\nFiles: src/a.ts, src/b.ts"},
    {"role": "assistant", "content": "Understood; reading the files."},
    {"role": "user", "content": "Now wire it and run the test."},
]
TOOLS = [{"type": "function", "function": {"name": "read_file",
          "description": "Read a file", "parameters": {"type": "object",
          "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
SAMPLE_PREFIX = "x-anthropic-billing-header: abc\nYou are a careful agent.\nTool specs here."

def current():
    return {
        "openai_messages_to_prompt": gw.openai_messages_to_prompt(MSGS, TOOLS),
        "lane_task_text": gw.lane_task_text(MSGS),
        "normalize_prefix": gw.normalize_prefix(SAMPLE_PREFIX),
    }

def main():
    cur = current()
    if os.getenv("REASONIX_FREEZE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        print("froze golden:", GOLDEN)
        return 0
    if not GOLDEN.exists():
        print("FAIL no golden — run once with REASONIX_FREEZE_GOLDEN=1 first")
        return 1
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    p = f = 0
    for k, v in cur.items():
        if v == want.get(k):
            p += 1; print(f"  ok   {k} byte-identical")
        else:
            f += 1; print(f"  FAIL {k} DIFFERS — cache-critical change!")
    print(f"\n{p} passed, {f} failed")
    return 1 if f else 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Freeze the golden from the CURRENT (pre-refactor) code**

Run: `REASONIX_FREEZE_GOLDEN=1 python3 tests/test-engine-seam-byte-identical.py`
Expected: `froze golden: .../engine_seam_golden.json`

- [ ] **Step 3: Run the test against current code — must pass (sanity)**

Run: `python3 tests/test-engine-seam-byte-identical.py`
Expected: `3 passed, 0 failed`

- [ ] **Step 4: Commit the safety net**

```bash
git add -f tests/test-engine-seam-byte-identical.py tests/fixtures/engine_seam_golden.json
git commit -m "test(gateway): engine-seam byte-identical golden (refactor safety net)"
```

---

### Task 2: Create the package skeleton + `env.py` (bottom layer)

**Files:**
- Create: `reasonix_gateway/__init__.py`
- Create: `reasonix_gateway/env.py`
- Modify: `reasonix-native-gateway.py` (import env from the package; remove the moved defs)

**Interfaces:**
- Produces: `reasonix_gateway.env` exporting `JSON`, `env_first`, `env_int`, `env_float`, `env_truthy` with their EXACT current signatures (`env_truthy(*names, default="")` etc.). `reasonix_gateway/__init__.py` re-exports them.

- [ ] **Step 1: Create `reasonix_gateway/env.py`** — cut lines 32–63 (the `JSON` alias + 4 env helpers) verbatim from `reasonix-native-gateway.py` into the new file. Add the needed imports at the top (`import os`, `from typing import Any`).

```python
# reasonix_gateway/env.py
import os
from typing import Any

JSON = dict[str, Any]

# (env_first, env_int, env_float, env_truthy — pasted VERBATIM from the old file, lines 37-63)
```

- [ ] **Step 2: Create `reasonix_gateway/__init__.py`** re-exporting env:

```python
from .env import JSON, env_first, env_int, env_float, env_truthy
```

- [ ] **Step 3: In `reasonix-native-gateway.py`, replace the moved defs with an import**

Delete lines 32–63 (the JSON alias + 4 env helpers); at the top of the file add:
```python
from reasonix_gateway.env import JSON, env_first, env_int, env_float, env_truthy
```
(Keep this import ABOVE the first use of any of them.)

- [ ] **Step 4: Run the full suite — must be unchanged**

Run: `for t in tests/test-*.py; do python3 "$t" >/dev/null 2>&1 || echo "FAIL $t"; done; echo done`
Expected: `done` with NO `FAIL` lines. Then: `python3 tests/test-engine-seam-byte-identical.py` → `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add -f reasonix_gateway/__init__.py reasonix_gateway/env.py reasonix-native-gateway.py
git commit -m "refactor(gateway): extract env helpers to reasonix_gateway/env.py"
```

---

### Task 2b: Extract `text.py` (shared leaf text helpers — the cycle-breaker)

**Files:**
- Create: `reasonix_gateway/text.py`
- Modify: `reasonix-native-gateway.py`, `reasonix_gateway/__init__.py`

**Interfaces:**
- Consumes: `reasonix_gateway.env` (Any/JSON).
- Produces: `reasonix_gateway.text` exporting `json_bytes`, `text_from_content`, `lane_task_text` (signatures UNCHANGED). These are the leaf helpers that harness, levers, AND engine_seam all need — putting them low keeps every import downward.

- [ ] **Step 1: Create `reasonix_gateway/text.py`** — cut VERBATIM `json_bytes`(808), `text_from_content`(812), `lane_task_text`(1366). Add imports:
```python
import json
from typing import Any
from .env import JSON
```
(`lane_task_text` calls `text_from_content`, both now in this same module — self-contained, no external lever/engine call.)

- [ ] **Step 2: Re-export from `__init__.py`**: `from .text import json_bytes, text_from_content, lane_task_text`.

- [ ] **Step 3: Delete the moved defs from `reasonix-native-gateway.py` + add the import** (below the env import).

- [ ] **Step 4: Run the full suite + byte test** — NO FAIL; `3 passed` (lane_task_text feeds the byte test, so this proves it moved byte-clean).

- [ ] **Step 5: Commit**

```bash
git add -f reasonix_gateway/text.py reasonix_gateway/__init__.py reasonix-native-gateway.py
git commit -m "refactor(gateway): extract shared text helpers to reasonix_gateway/text.py"
```

---

### Task 3: Extract `harness.py` (leaf helpers)

**Files:**
- Create: `reasonix_gateway/harness.py`
- Modify: `reasonix-native-gateway.py`, `reasonix_gateway/__init__.py`

**Interfaces:**
- Consumes: `reasonix_gateway.env` (JSON, env_truthy, os.getenv via env helpers).
- Produces: `reasonix_gateway.harness` exporting `_lane_fail_marker_on`, `lane_unverified_reply`, `_lane_harness_on`, `parse_harness_result`, `harness_lane_reply`, `lane_acceptance_test`, `_clean_acceptance_command` (and the `_PROSE_CUT_RE` regex). Signatures UNCHANGED.

- [ ] **Step 1: Create `reasonix_gateway/harness.py`** — cut the 7 functions (lines 65–198 region: `_lane_fail_marker_on` 65, `lane_unverified_reply` 70, `_lane_harness_on` 90, `parse_harness_result` 95, `harness_lane_reply` 111, `lane_acceptance_test` 123, `_clean_acceptance_command` 144) + the `_PROSE_CUT_RE` regex (138) VERBATIM. Add imports:
```python
import re
from .env import JSON, env_truthy
from .text import lane_task_text
```
NOTE: `lane_acceptance_test` calls `lane_task_text`, which now lives in the LOW `text.py` module (extracted in Task 2b, before this task) — so harness imports it DOWNWARD, no lazy import, no cycle.

- [ ] **Step 2: Re-export from `__init__.py`** — add:
```python
from .harness import (_lane_fail_marker_on, lane_unverified_reply, _lane_harness_on,
    parse_harness_result, harness_lane_reply, lane_acceptance_test, _clean_acceptance_command)
```

- [ ] **Step 3: In `reasonix-native-gateway.py`, delete the moved defs + add the import** (place import below the env import).

- [ ] **Step 4: Run the full suite + byte test** (same command as Task 2 Step 4) — NO FAIL lines; `3 passed`. Confirm `python3 tests/test-harness-result.py` and `tests/test-harness-honest-fixes.py` still pass (they exercise these helpers).

- [ ] **Step 5: Commit**

```bash
git add -f reasonix_gateway/harness.py reasonix_gateway/__init__.py reasonix-native-gateway.py
git commit -m "refactor(gateway): extract harness helpers to reasonix_gateway/harness.py"
```

---

### Task 4: Extract `cost.py` (leaf helpers)

**Files:**
- Create: `reasonix_gateway/cost.py`
- Modify: `reasonix-native-gateway.py`, `reasonix_gateway/__init__.py`

**Interfaces:**
- Consumes: `reasonix_gateway.env` (JSON, env helpers).
- Produces: `reasonix_gateway.cost` exporting `weighted_cache`, `classify_miss`, `append_reasonix_cost`, `summarize_reasonix_cost`. Signatures UNCHANGED.

- [ ] **Step 1: Create `reasonix_gateway/cost.py`** — cut `weighted_cache`(2044), `classify_miss`(2066), `append_reasonix_cost`(2086), `summarize_reasonix_cost`(2116) VERBATIM. Add imports:
```python
import json, time
from pathlib import Path
from .env import JSON, env_first
```
(Include any other stdlib import these 4 functions use — check the function bodies; add exactly what they reference.)

- [ ] **Step 2: Re-export from `__init__.py`**:
```python
from .cost import weighted_cache, classify_miss, append_reasonix_cost, summarize_reasonix_cost
```

- [ ] **Step 3: Delete the moved defs from `reasonix-native-gateway.py` + add the import.**

- [ ] **Step 4: Run the full suite + byte test** — NO FAIL; `3 passed`. Confirm a cost-related test still passes (e.g. `grep -l append_reasonix_cost tests/*.py` then run those).

- [ ] **Step 5: Commit**

```bash
git add -f reasonix_gateway/cost.py reasonix_gateway/__init__.py reasonix-native-gateway.py
git commit -m "refactor(gateway): extract cost ledger to reasonix_gateway/cost.py"
```

---

### Task 5: Extract `levers.py` (the lever layer — biggest move)

**Files:**
- Create: `reasonix_gateway/levers.py`
- Modify: `reasonix-native-gateway.py`, `reasonix_gateway/__init__.py`

**Interfaces:**
- Consumes: `reasonix_gateway.env`, `reasonix_gateway.harness` (if any lever calls a harness helper — check; likely none).
- Produces: `reasonix_gateway.levers` exporting EVERY lever helper + `classify_lane_type`, `is_synthesis_prompt`, `is_heavy_synthesis`, the `*_directive`/`*_instruction` builders, `read_cache_injection_block`, `populate_read_cache`, `overscope_rejection`, `_strip_injected_guide`, `_bulk_scope_match`, `lane_file_scope_count`, `extract_file_paths_from_prompt`, `acquire_prime_role`, `record_keepalive_prefix`, `keepalive_targets`, `register_lane_attempt`, `should_force_fallback`, `clear_lane_count`, `build_preindex`, `reasonix_cli_semaphore`, `gateway_trace`, etc. — with their module-level state (locks/dicts/regexes listed in the module map). Signatures UNCHANGED.

- [ ] **Step 1: Create `reasonix_gateway/levers.py`** — cut, VERBATIM, every lever function + its module-level state. Use the module map's `levers.py` row as the checklist. The cut ranges are roughly: 138-additional-state, 199–328 (preindex), 329–365 + 680–724 + 763–796 (prime/semaphore/loop-breaker), 394–460 (keepalive), 461–678 (read-cache), 1361–1620 (classify/synthesis/directives/read-summary), 1677–1783 (overscope). Add imports at top:
```python
import os, re, json, threading, time, hashlib
from pathlib import Path
from .env import JSON, env_first, env_int, env_float, env_truthy
from .text import text_from_content, lane_task_text
```
RULE: a function goes here if engine_seam CALLS it but it does NOT call anything in engine_seam. Lever functions that need `lane_task_text`/`text_from_content` import them from `.text` (the low module) — DOWNWARD, no lazy import, no cycle.

- [ ] **Step 2: Re-export from `__init__.py`** — add a `from .levers import (...)` listing every public name moved (the ones the old file or tests reference; grep `tests/` for lever names to be sure none is missed).

- [ ] **Step 3: Delete the moved defs + state from `reasonix-native-gateway.py` + add the import.**

- [ ] **Step 4: Run the full suite + byte test** — NO FAIL; `3 passed`. Specifically run the lever tests: `tests/test-overscope-*.py`, `tests/test-read-summary.py`, `tests/test-output-discipline.py`, `tests/test-read-cache-bytestable.py`, `tests/test-overscope-guide-strip.py` — all pass.

- [ ] **Step 5: Commit**

```bash
git add -f reasonix_gateway/levers.py reasonix_gateway/__init__.py reasonix-native-gateway.py
git commit -m "refactor(gateway): extract levers to reasonix_gateway/levers.py"
```

---

### Task 6: Extract `engine_seam.py` (THE byte-critical move — most careful)

**Files:**
- Create: `reasonix_gateway/engine_seam.py`
- Modify: `reasonix-native-gateway.py`, `reasonix_gateway/__init__.py`

**Interfaces:**
- Consumes: `reasonix_gateway.env`, `reasonix_gateway.levers` (classify_lane_type, the *_directive/*_instruction builders, read_cache_injection_block, overscope_rejection), `reasonix_gateway.harness` (lane_unverified_reply, etc. if called).
- Produces: `reasonix_gateway.engine_seam` exporting `openai_messages_to_prompt`, `run_reasonix_acp`, `normalize_prefix`, `anthropic_messages_to_openai`, `anthropic_tools_to_openai`, `anthropic_tool_choice_to_openai`, `openai_response_to_anthropic`, `provider_chat_payload`, `call_openai_compatible`, `call_openai_chat_completion`, `text_from_content`, `estimate_tokens`, `json_bytes`, `model_registry`, the structured-output helpers (`structured_output_prompt_instruction`, `tool_schema_entries`, `requested_structured_output_tool`, etc.), `anthropic_tool_use_response`, `anthropic_end_turn_response`, `retry_cap_for_empty`, `gateway_trace`. Signatures UNCHANGED.

- [ ] **Step 1: Run the byte test BEFORE the move to confirm green baseline**

Run: `python3 tests/test-engine-seam-byte-identical.py`
Expected: `3 passed, 0 failed`

- [ ] **Step 2: Create `reasonix_gateway/engine_seam.py`** — cut VERBATIM all the engine-seam functions (module map `engine_seam.py` row). Add imports:
```python
import os, json, re, subprocess, time, hashlib
from pathlib import Path
from .env import JSON, env_first, env_int, env_float, env_truthy
from .text import text_from_content, lane_task_text, json_bytes
from .levers import (classify_lane_type, output_discipline_directive, context_budget_directive,
    mapreduce_directive, read_cache_injection_block, read_lane_summary_instruction,
    overscope_rejection, populate_read_cache, acquire_prime_role, record_keepalive_prefix,
    register_lane_attempt, should_force_fallback, clear_lane_count, reasonix_cli_semaphore,
    output_discipline_budget, read_summary_budget)
from .harness import lane_unverified_reply
```
(Adjust the `from .levers import (...)` to EXACTLY the lever names this module's functions call — grep the moved bodies for the call sites; import precisely those.)

- [ ] **Step 3: Re-export from `__init__.py`** — add `from .engine_seam import (...)` for every public name.

- [ ] **Step 4: Delete the moved defs from `reasonix-native-gateway.py` + add the import.**

- [ ] **Step 5: Run the byte test AFTER the move — THE critical gate**

Run: `python3 tests/test-engine-seam-byte-identical.py`
Expected: `3 passed, 0 failed`. If ANY line says DIFFERS, the move changed a byte — revert and find the diff (usually a reordered import side-effect or a lost module-level constant). Do NOT proceed until byte-identical.

- [ ] **Step 6: Run the full suite** — NO FAIL lines.

- [ ] **Step 7: Commit**

```bash
git add -f reasonix_gateway/engine_seam.py reasonix_gateway/__init__.py reasonix-native-gateway.py
git commit -m "refactor(gateway): extract engine-seam to reasonix_gateway/engine_seam.py (byte-identical)"
```

---

### Task 7: Extract `server.py` + reduce the old file to a thin shim

**Files:**
- Create: `reasonix_gateway/server.py`
- Modify: `reasonix-native-gateway.py` (→ thin shim), `reasonix_gateway/__init__.py`

**Interfaces:**
- Consumes: everything (env, levers, engine_seam, cost, harness).
- Produces: `reasonix_gateway.server` exporting `GatewayError`, `ClientGone`, `Handler`, `main`, `_keepalive_loop`. After this, `reasonix-native-gateway.py` is a thin re-export shim.

- [ ] **Step 1: Create `reasonix_gateway/server.py`** — cut VERBATIM `GatewayError`(2737), `ClientGone`(2745), `Handler`(2751), `_keepalive_loop`(3160), `main`(3185). Add imports for everything the Handler/main reference, from the sibling modules:
```python
import os, sys, json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .env import JSON, env_first, env_int, env_truthy
from .engine_seam import (run_reasonix_acp, call_openai_compatible, call_openai_chat_completion,
    anthropic_messages_to_openai, openai_response_to_anthropic, json_bytes, ...)
from .levers import (...)   # whatever Handler/_keepalive_loop call
from .cost import append_reasonix_cost, summarize_reasonix_cost
```
(Fill the `import (...)` lists by grepping the Handler/main bodies for call sites.)

- [ ] **Step 2: Re-export from `__init__.py`**: `from .server import GatewayError, ClientGone, Handler, main`.

- [ ] **Step 3: Replace `reasonix-native-gateway.py` ENTIRELY with the thin shim:**

```python
#!/usr/bin/env python3
"""Compatibility shim: the gateway now lives in the reasonix_gateway package.
This path is kept stable because the launcher, MCP, install.sh, and the test suite
import from here. Everything is re-exported from reasonix_gateway."""
import sys
from reasonix_gateway import *  # noqa: F401,F403  (re-export the full public surface)
from reasonix_gateway.server import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the FULL suite + byte test + guard**

Run:
```bash
for t in tests/test-*.py; do python3 "$t" >/dev/null 2>&1 || echo "FAIL $t"; done; echo done
python3 tests/test-engine-seam-byte-identical.py
python3 tests/test-no-codex-leftovers.py
python3 -c "import ast; ast.parse(open('reasonix-native-gateway.py').read()); print('shim ast ok')"
```
Expected: `done` (no FAIL), `3 passed`, `PASS: no codex leftovers`, `shim ast ok`.

- [ ] **Step 5: Smoke-test the gateway still boots** — confirm the shim path runs:
```bash
python3 -c "import importlib.util; s=importlib.util.spec_from_file_location('g','reasonix-native-gateway.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print('boot ok:', callable(m.main), callable(m.run_reasonix_acp))"
```
Expected: `boot ok: True True`

- [ ] **Step 6: Commit**

```bash
git add -f reasonix_gateway/server.py reasonix_gateway/__init__.py reasonix-native-gateway.py
git commit -m "refactor(gateway): extract server + reduce old path to thin shim"
```

---

### Task 8: Whole-refactor verification on real DeepSeek (the cache gate)

**Files:** none (verification only).

- [ ] **Step 1: Reinstall the refactored gateway into the install home** (so the bench runs the new code). Kill the stale gateway first (a long-lived process holds old code):

```bash
pkill -9 -f reasonix-native-gateway.py 2>/dev/null; rm -f ~/.claude/reasonix-fleet/runtime/gateway.*.port 2>/dev/null
bash install.sh 2>&1 | tail -5
```
NOTE: install.sh must copy the new `reasonix_gateway/` package too — if it only copies `reasonix-native-gateway.py`, the install will have a dangling import. CHECK install.sh's copy list; if the package isn't copied, that is a real gap to fix here (add `cp -R reasonix_gateway "$INSTALL_HOME/"`), commit it, and note it.

- [ ] **Step 2: Run realworld-bench on real DeepSeek**

Run: `python3 runtime/realworld-bench.py 2>&1 | tail -20`
Expected: ALL GATES PASS (review cache ~99%, fan-out ~91%). If cache regressed, the engine-seam is NOT byte-identical despite the unit byte test — investigate (the bench exercises paths the golden fixture may not cover).

- [ ] **Step 3: Final commit (if install.sh needed the package-copy fix)**

```bash
git add -f install.sh && git commit -m "fix(install): copy reasonix_gateway package" || echo "no install change needed"
```

---

## Notes for the executor
- Every task is a PURE MOVE. If making a test pass requires editing a function's LOGIC, stop — that's a behavior change and a bug in the move (usually a missing import or a lost module-level constant).
- The byte test (Task 1) is the safety net — run it after EVERY task, not just Task 6.
- Lazy imports (`from .engine_seam import x` inside a function body) are the sanctioned way to break the one cycle (harness/levers needing `lane_task_text`). Keep them rare and commented.
- Don't reformat moved code — paste it verbatim so the diff is a clean move (easier to review, and guarantees byte-identical behavior).
