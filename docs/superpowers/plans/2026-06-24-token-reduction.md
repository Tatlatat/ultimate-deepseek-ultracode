# Token-Reduction Experiment System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build an experiment harness + 6 env-flag-gated token-reduction levers for the reasonix fan-out workflow, each measured before promotion, targeting the measured cost split (output 42.3%, miss 38.1%) without quality loss.

**Architecture:** A harness (`runtime/lever-matrix-bench.py`) runs ONE fixed workload through a lever on/off matrix, measuring cache%/input/output/cost/quality. Levers live in the gateway (`reasonix-native-gateway.py`), the shim (`engine/run-lane.mjs`), the Workflow hook (`hooks/reasonix-workflow.py`), or the fork (`Documents/reasonix-fork/src`, re-vendored). Spec: `docs/superpowers/specs/2026-06-24-token-reduction-experiment-design.md`.

**Tech Stack:** Python gateway/harness, Node ESM shim, TypeScript fork (vendored), DeepSeek-v4-flash.

## Global Constraints

- **Every lever defaults OFF (`0`)** — measure-then-promote (owner decision Q1).
- **NEVER truncate context** to save tokens (rejected anti-pattern). Reduce by decompose/summarize/retrieve.
- **Byte-stable prefix is sacred:** the shared prefix (codeSystemPrompt + toolSpecs = 14K tok = 73.5% of input) is byte-identical and drives the 96.3% cache. Any lever that injects into the prompt (C, E, D-digest) MUST insert at a FIXED boundary (after the shared system block, before the per-lane tail), be byte-deterministic, and ship a unit test asserting two prompts differing only in the tail produce identical injected-prefix bytes. Break this = cache regression.
- **ONE shared classifier + ONE shim line** for F+A (cross-lever risk): build `classify_lane_type()` once (gateway) and add `maxOutputTokens` to the request dict once (gateway:1316 → run-lane.mjs:169 → loop.ts:963, all VERIFIED already wired — no fork rebuild for F/A). F owns the budget table; A's read=512 is F's READ budget.
- **`lane_type` ledger field** added ONCE in `append_reasonix_cost` (gateway:1167-1169); A+F+G all consume it.
- **C must live in the gateway** (long-lived process), NOT the shim (run-lane.mjs:168 `session:undefined` → each lane is an ephemeral subprocess; a per-process cache shares nothing). C persists to `runtime/read-summary-cache.json` (Q10).
- **Measured budgets** (from ledger, Q3): READ=512, EDIT=ceil(edit-lane-P95×1.2)≈5900 (re-measure P95 of EDIT-classified lanes during Task 3), DEFAULT=2048.
- **Probe before capping edit lanes** (Q2): run a deliberate low-max_tokens edit lane and confirm the cap doesn't cut mid-SEARCH/REPLACE before enabling any edit-lane output cap.
- **B+D batch one fork rebuild** (Q6) via the Sub-project-2 `build:engine` + re-vendor flow.
- **Harness:** fixed config order + a shared warm-up lane before each config (Q8); `best_combo` = auto-union of default-ON flags (Q9).
- **git-status discipline** after any subagent step, in BOTH repos.
- All Python parses (`python3 -c "import ast;ast.parse(...)"`); launcher/bench `bash -n` clean; the existing suite + guard stay green; `tests/test-reasonix-fleet.sh` exits 0.

---

### Task 1: Harness backbone — `lever-matrix-bench.py` + ledger plumbing

**Files:**
- Create: `runtime/lever-matrix-bench.py`
- Modify: `runtime/realworld-bench.py:141-143` (add output_tokens + median already done — add per-type), `reasonix-native-gateway.py:1154-1169` (add `lane_type` field)
- Test: the harness IS the test infra; add `tests/test-lever-matrix.py` (unit-tests the matrix logic with a MOCK gateway, no real DeepSeek)

**Interfaces:**
- Produces: `run_matrix(configs) -> table` where each row = `{config, cache_weighted, cache_median, input_tok, output_tok, est_cost, quality}`; `WORKLOAD_SPEC` (read+edit+review+workflow lanes); est_cost using price ratio hit:miss:out = 1:51:101 (owner split).
- Consumes: `realworld-bench.start_gateway`, `ledger_window`, `grade`.

- [ ] **Step 1: Write the failing test for the cost model + matrix shape**

`tests/test-lever-matrix.py`:
```python
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("lmb", Path(__file__).resolve().parent.parent/"runtime"/"lever-matrix-bench.py")
lmb = importlib.util.module_from_spec(spec); spec.loader.exec_module(lmb)

def test_cost_model_weights_output_101x():
    # est_cost must price output ~101x a cache-hit and miss ~51x (owner split)
    c_hit  = lmb.est_cost(input_tok=1000, cache_pct=100, output_tok=0)
    c_out  = lmb.est_cost(input_tok=0,    cache_pct=0,   output_tok=1000)
    assert c_out / c_hit > 90, f"output must be ~101x a hit; got {c_out/c_hit:.0f}x"

def test_matrix_has_baseline_first():
    cfgs = lmb.build_matrix(levers=["OUTPUT_DISCIPLINE"])
    assert cfgs[0]["name"] == "baseline" and cfgs[0]["flags"] == {}

if __name__ == "__main__":
    test_cost_model_weights_output_101x(); test_matrix_has_baseline_first()
    print("PASS: lever-matrix unit")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 tests/test-lever-matrix.py`
Expected: FAIL — `No module named ...lever-matrix-bench` / `est_cost not defined`.

- [ ] **Step 3: Write `runtime/lever-matrix-bench.py`**

Reuse `realworld-bench` internals. Implement: `est_cost(input_tok, cache_pct, output_tok)` with `P_HIT=1, P_MISS=51, P_OUT=101` relative units (`hit=input*cache; miss=input*(1-cache); cost=hit*1+miss*51+output*101`); `WORKLOAD_SPEC` = 8 READ lanes (StructuredOutput `{summary,file}`), 2 EDIT lanes (write/modify a scratch file), 6 REVIEW lanes (shared 12K block + 1-word suffix) + 1 WORKFLOW-shaped lane (routes through the hook); `build_matrix(levers)` returns `[{name:"baseline",flags:{}}, {name:lever,flags:{<FLAG>:"1"}} for each, {name:"best_combo",flags:<union default-ON>}]`; `run_matrix` spawns a gateway per config with the flags as env, runs WORKLOAD, reads `ledger_window` (weighted+median+output), grades quality, prints a table. A shared warm-up lane runs before each config.

- [ ] **Step 4: Add `lane_type` to the cost ledger**

In `reasonix-native-gateway.py:1154-1169` `append_reasonix_cost`, add a `lane_type` parameter (default `"unknown"`) and write `"lane_type": lane_type` into the record. Caller (`run_reasonix_acp`) passes the classification (Task 2 provides it; until then pass `"unknown"`).

- [ ] **Step 5: Run the unit test**

Run: `python3 tests/test-lever-matrix.py`
Expected: `PASS: lever-matrix unit`.

- [ ] **Step 6: Run the harness once for a BASELINE row (real DeepSeek)**

Run: `pkill -f reasonix-native-gateway.py; python3 runtime/lever-matrix-bench.py --only baseline`
Expected: a baseline row prints with cache ~96%, and records lanes to the ledger with `lane_type`. Capture this row — it's the reference for every lever.

- [ ] **Step 7: Guard + suite + commit**

Run: `python3 tests/test-no-codex-leftovers.py` (PASS); `python3 -c "import ast;ast.parse(open('runtime/lever-matrix-bench.py').read())"` (OK).
```bash
git add -A && git commit -m "feat(harness): lever-matrix-bench + lane_type ledger field (baseline measured)"
```

---

### Task 2: Shared plumbing — `classify_lane_type()` + `maxOutputTokens` forwarding

**Files:**
- Modify: `reasonix-native-gateway.py` (new `classify_lane_type` near :803; add `maxOutputTokens` to request dict :1316; pass `lane_type` to `append_reasonix_cost`), `engine/run-lane.mjs:169` (read `req.maxOutputTokens`)
- Test: `tests/test-lane-classify.py`

**Interfaces:**
- Produces: `classify_lane_type(tools, prompt_text) -> 'read'|'edit'|'synthesize'|'unknown'`; the request dict carries `maxOutputTokens` when set; the shim passes it to `CacheFirstLoop`.
- Consumes: `_READER_INTENT_RE` (:785), `_SYNTHESIS_INTENT_RE` (:778), `is_heavy_synthesis` (:803).

- [ ] **Step 1: Write the failing classifier test**

`tests/test-lane-classify.py`:
```python
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("gw", Path(__file__).resolve().parent.parent/"reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(spec); spec.loader.exec_module(gw)
def expect(c,m):
    if not c: raise SystemExit(f"FAIL: {m}")
def test():
    expect(gw.classify_lane_type(None, "Read ONLY foo.py and summarize its purpose")=="read", "read intent")
    expect(gw.classify_lane_type(None, "Edit bar.py: add a function baz()")=="edit", "edit intent")
    expect(gw.classify_lane_type(None, "Synthesize and merge these findings into one object")=="synthesize", "synth intent")
    print("PASS: lane classify")
if __name__=="__main__": test()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tests/test-lane-classify.py`
Expected: FAIL — `classify_lane_type not defined`.

- [ ] **Step 3: Implement `classify_lane_type`**

Near gateway:803, add `_EDIT_INTENT_RE = re.compile(r"\b(edit|write|create|modify|apply|patch|implement|add|delete|rename|refactor)\b", re.I)` and:
```python
def classify_lane_type(tools, prompt_text):
    if is_heavy_synthesis(tools, len(prompt_text or ""), prompt_text or ""):
        return "synthesize"
    if _SYNTHESIS_INTENT_RE.search(prompt_text or ""):
        return "synthesize"
    if _EDIT_INTENT_RE.search(prompt_text or ""):
        return "edit"
    if _READER_INTENT_RE.search(prompt_text or ""):
        return "read"
    return "unknown"
```
(Edit checked before read so "modify and summarize" → edit, never capped as read.)

- [ ] **Step 4: Forward `maxOutputTokens` through the request dict + shim**

In `run_reasonix_acp` (gateway:1316 request dict), nothing is added YET (F/A set it in their tasks) — but wire the PLUMBING: accept an optional `max_output_tokens` local (default None) and `if max_output_tokens: request["maxOutputTokens"] = max_output_tokens`. Classify the lane (`lane_type = classify_lane_type(...)`) and pass `lane_type` to `append_reasonix_cost`. In `engine/run-lane.mjs:162-170` add `maxOutputTokens: req.maxOutputTokens ?? undefined,` to the `CacheFirstLoop` options.

- [ ] **Step 5: Run the classifier test + a shim mock test**

Run: `python3 tests/test-lane-classify.py` (PASS).
Run: `node tests/test-engine-shim.mjs` (PASS — shim still honors the I/O contract; maxOutputTokens optional).

- [ ] **Step 6: Integration + guard + commit**

Run: `pkill -f reasonix-native-gateway.py; bash tests/test-reasonix-fleet.sh` (exit 0); `python3 tests/test-no-codex-leftovers.py` (PASS).
```bash
git add -A && git commit -m "feat(plumbing): shared classify_lane_type + maxOutputTokens forwarding (F+A substrate)"
```

---

### Task 3: Lever F — Output discipline (#1 ROI: the 42.3% output bucket)

**Files:**
- Modify: `reasonix-native-gateway.py` (`output_discipline_directive()`, budget application in `run_reasonix_acp`)
- Test: `tests/test-output-discipline.py` + a real probe lane (Q2)

**Interfaces:**
- Env: `CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE=0` (default off, Q1); `_MAX_TOKENS_EDIT` (default = measured P95×1.2), `_READ=512`, `_DEFAULT=2048`; `_DIRECTIVE=1` (the narration-ban + diff-only text).
- Consumes: `classify_lane_type` (Task 2), the `maxOutputTokens` plumbing.

- [ ] **Step 1: PROBE — does max_tokens cut mid-SEARCH/REPLACE? (Q2, blocking)**

Run a one-off real lane with a low max_tokens on an edit task and inspect whether the returned edit block is structurally complete:
```bash
pkill -f reasonix-native-gateway.py
CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE=1 CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE_MAX_TOKENS_EDIT=200 \
  python3 runtime/lever-matrix-bench.py --only probe-edit-cap
```
Record: does the edit lane return a parseable edit or a truncated/failed one? If the cap truncates mid-block (edit fails), set the EDIT budget conservatively (≥ measured P95×1.2) and NEVER below the structural minimum; document the finding. This gates whether the edit cap is safe.

- [ ] **Step 2: Measure EDIT-lane P95 from the ledger (Q3)**

Run: `python3 -c "import json,statistics as st; rows=[json.loads(l) for l in open('runtime/reasonix-cost.jsonl') if l.strip()]; e=[r['output_tokens'] for r in rows if r.get('lane_type')=='edit' and isinstance(r.get('output_tokens'),int)]; print('edit P95:', int(st.quantiles(e,n=100)[94]) if len(e)>=100 else (max(e) if e else 'n/a'))"`
Set `_MAX_TOKENS_EDIT` default = `ceil(that × 1.2)`. (If too few edit lanes yet, use 5900 from the top-20% proxy and re-tune after the harness generates edit lanes.)

- [ ] **Step 3: Write the failing directive + budget test**

`tests/test-output-discipline.py`: assert (a) `output_discipline_directive()` returns "" when the flag is off and a non-empty narration-ban + diff-only text when on; (b) the budget selector maps `read→512, edit→<P95×1.2>, unknown→2048`.

- [ ] **Step 4: Run to verify it fails** — `python3 tests/test-output-discipline.py` → FAIL.

- [ ] **Step 5: Implement F**

`output_discipline_directive()` (~25 lines): when `CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE=1`, return a block appended LAST in the prompt: "Be terse. No narration ('I will now…'), no restating the task, no chain-of-thought prose. For edits: emit a minimal unified diff / SEARCH-REPLACE only — NEVER reprint unchanged code, NEVER write placeholder comments like '// rest unchanged'." In `run_reasonix_acp`, when the flag is on, compute `max_output_tokens` from the lane_type budget table and pass it through the Task-2 plumbing. The directive is the soft layer; the budget is the hard layer.

- [ ] **Step 6: Run the unit test + harness F-on vs baseline**

Run: `python3 tests/test-output-discipline.py` (PASS).
Run: `pkill -f reasonix-native-gateway.py; python3 runtime/lever-matrix-bench.py --only baseline,OUTPUT_DISCIPLINE`
Expected: a table comparing baseline vs F. Gate HARD: `edit_correct` must hold and edit-lane hollow-rate ≤2% (F must not break edits). Output tokens should drop; quality must not.

- [ ] **Step 7: Guard + commit**

```bash
git add -A && git commit -m "feat(lever F): output discipline — lane-type max_tokens budget + narration-ban/diff-only directive (default off)"
```

---

### Task 4: Lever A — Schema-enforced read summary (#2; thin add on F)

**Files:** Modify `reasonix-native-gateway.py` (`read_lane_summary_instruction()`); Test `tests/test-read-summary.py`.

**Interfaces:** Env `CLAUDE_REASONIX_GATEWAY_READ_SUMMARY=0` + `_READ_SUMMARY_MAX_TOKENS=512`. Read lanes get a forced `{findings,files_read,flag}` JSON instruction + the 512 cap. Measures the second-order synthesize-input drop.

- [ ] **Step 1: Write the failing test** — `read_lane_summary_instruction()` returns "" off, and the fixed schema `{findings,files_read,flag}` text on; only fires for `lane_type=='read'`.
- [ ] **Step 2: Run to verify it fails** — `python3 tests/test-read-summary.py` → FAIL.
- [ ] **Step 3: Implement A** — a sibling of `structured_output_prompt_instruction()`: when `READ_SUMMARY=1` and lane is `read`, append the fixed-schema instruction (Q4) LAST in the prompt and cap output at 512 via the plumbing. Mutually exclusive with an injected StructuredOutput tool (don't double-inject).
- [ ] **Step 4: Run unit test** — PASS.
- [ ] **Step 5: Harness A-on, measure BOTH drops** — run `--only baseline,READ_SUMMARY`. Measure the read lane's own output drop AND the downstream synthesize lane's INPUT drop (the real prize: read output → synth input). Report marginal gain over F (A overlaps F's READ budget).
- [ ] **Step 6: Guard + commit** — `git commit -m "feat(lever A): schema-enforced read summary (fixed {findings,files_read,flag}, 512 cap)"`.

---

### Task 5: Lever C — Gateway shared read-cache (miss→hit on re-reads)

**Files:** Modify `reasonix-native-gateway.py` (module-level `_READ_SUMMARY_CACHE` + persist to `runtime/read-summary-cache.json`); Test `tests/test-read-cache-bytestable.py` (BLOCKING byte-stability gate).

**Interfaces:** Env `CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE=0` + `_CAP=512`/`_TTL_S=300`/`_MAX_BYTES=131072`. A file read+summarized by one lane is cached (key = path + mtime/hash) and injected as a cached-summary block into later lanes at a FIXED prefix boundary. Persists to disk (Q10).

- [ ] **Step 1: Write the BLOCKING byte-stability test FIRST** — two prompts differing only in their per-lane tail, with the same cached-summary block injected, must produce BYTE-IDENTICAL injected-prefix bytes. This is the gate: if injection isn't byte-deterministic it breaks the 73.5% shared cache.
- [ ] **Step 2: Run to verify it fails** — FAIL (cache/inject not implemented).
- [ ] **Step 3: Implement C in the gateway** — `_READ_SUMMARY_CACHE` dict + lock + `_evict_oldest` (mirror `_PRIME_GATES` :87) + persist/load `runtime/read-summary-cache.json` with mtime-freshness; `extract_file_paths_from_prompt`; lookup→inject (at the FIXED boundary, sorted, normalize_prefix-clean) → populate after the lane returns. Lives in the gateway (long-lived), NOT the shim.
- [ ] **Step 4: Run the byte-stability test** — PASS (this gates everything else).
- [ ] **Step 5: Harness Scenario C2 — twice-run fan-out** — run the same fan-out workload TWICE under C-on; assert run-2 cache ≥ run-1 + 5pts (the cache converted misses to hits). Verify with PREFIX_TRACE that the injected block didn't fork the prime-gate family.
- [ ] **Step 6: Guard + commit** — `git commit -m "feat(lever C): gateway shared read-summary cache (persisted, byte-stable injection)"`.

---

### Task 6: Lever B — Sub-agent read-in-isolation (fork change, re-vendor)

**Files:** Modify `Documents/reasonix-fork/src/code/setup.ts` (new `read_file_isolated` tool + `read_file` description nudge); rebuild + re-vendor `vendor/reasonix-engine/`; Test `tests/test-read-isolated.mjs`.

**Interfaces:** Env `REASONIX_READ_ISOLATED=0`. A `read_file_isolated` tool spawns a child loop (`spawnSubagent`, fork src/tools/subagent.ts) that reads in a separate context and returns only a ≤2K summary; the parent never ingests the raw file.

- [ ] **Step 1: Write the failing fork test** — in the fork, a test that the `read_file_isolated` tool exists in `buildCodeToolset` specs when the flag is on and returns a summary (not raw) for a known file.
- [ ] **Step 2: Run to verify it fails** — `npx vitest run` in the fork → FAIL.
- [ ] **Step 3: Implement B in the fork** — ~30-line tool block in setup.ts reusing the existing `subagentClient` closure + `EXPLORE_SYSTEM`; add a nudge to `read_file`'s description ("for large files, prefer read_file_isolated"). Build (`npm run build:engine`).
- [ ] **Step 4: Re-vendor + verify** — copy the built `dist-engine` into `vendor/reasonix-engine/dist` (Sub-project-2 flow); `node -e import` exports check; `bash tests/test-reasonix-fleet.sh` exit 0.
- [ ] **Step 5: Harness B-on — measure parent in_tok drop + ADOPTION rate** — does the model actually CALL the isolated tool? Adoption is the make-or-break metric. Measure parent lane input drop when it does.
- [ ] **Step 6: Guard + commit (both repos)** — fork commit + fleet commit `feat(lever B): sub-agent read-in-isolation tool (re-vendored)`.

---

### Task 7: Lever E — Speculative prefetch (advisory mode first, Q7)

**Files:** Modify `hooks/reasonix-workflow.py` (prefetch in the Workflow hook), `reasonix-native-gateway.py:1294` (read `LANE_SYSTEM_APPEND`); Test `tests/test-prefetch-precision.py`.

**Interfaces:** Env `CLAUDE_REASONIX_PREFETCH_CONTEXT=off|advisory|inject` (default off) + `_MAX_FILES=8`/`_FILE_CAP_BYTES=32768`/`_TIMEOUT=20`. From the workflow script, predict files each lane needs, summarize once, and (in `inject` mode) place them in the byte-stable shared prefix. **Advisory mode ships first** (predict + log precision, no prefix change), promote to inject only if precision is high.

- [ ] **Step 1: Write the failing precision test** — `predict_prefetch_files(workflow_script, cwd)` returns a bounded list (≤MAX_FILES) of real files referenced; a precision metric (predicted ∩ actually-read / predicted) is computed in advisory mode.
- [ ] **Step 2: Run to verify it fails** — FAIL.
- [ ] **Step 3: Implement E advisory mode** — `predict_prefetch_files` (grep task for filenames/symbols, ~50 lines); in advisory mode, log predicted vs the files lanes actually read (no prefix change, zero risk). NO grep-symbol fallback by default (Q7).
- [ ] **Step 4: Run unit test** — PASS.
- [ ] **Step 5: Harness E-advisory — measure prediction PRECISION on the workflow-shaped lane** — only promote to a (later) inject mode if precision clears a bar; record the number. Inject mode (if pursued) reuses the C byte-stability test.
- [ ] **Step 6: Guard + commit** — `feat(lever E): speculative prefetch — advisory mode (precision measured)`.

---

### Task 8: Lever D — Pre-index (LAST; fork change, embedding provider)

**Files:** Modify `Documents/reasonix-fork/src/index.ts` (export `buildIndex`/`indexCompatible`), `reasonix-native-gateway.py` (`build_preindex` as SOLE build trigger); rebuild + re-vendor (batch with B if B not yet shipped); Test `tests/test-preindex.py`.

**Interfaces:** Env `CLAUDE_REASONIX_PREINDEX=0` + `_TIMEOUT=120` + `REASONIX_EMBED_*`. Build the semantic index ONCE per codebase (gateway is the sole build trigger — per-lane is read-only `indexCompatible()` to avoid the JSONL append race), exposed via the EXISTING `semantic_search` query tool (NO prefix injection — sidesteps byte-stability). Fail-open if the embedding provider is absent.

- [ ] **Step 1: Verify the embedding provider exists** — Q5: owner has one from a prior semantic-search project. Confirm: `command -v ollama && ollama list | grep -i embed` OR the configured `REASONIX_EMBED_BASE_URL` responds. If absent → D fails open (skip, log) — do not block other levers.
- [ ] **Step 2: Write the failing index test** — `build_preindex(cwd)` produces an index; a lane with PREINDEX on has `semantic_search` in its toolSpecs; per-lane path is read-only (no double-build race).
- [ ] **Step 3: Run to verify it fails** — FAIL.
- [ ] **Step 4: Implement D in the fork + gateway** — export `buildIndex`/`indexCompatible` from index.ts; rebuild + re-vendor (batch with B's rebuild if pending); gateway `build_preindex` is the sole trigger, per-lane only checks `indexCompatible()`.
- [ ] **Step 5: Harness D-on — measure read-exploration lane input drop** — only on read-exploration lanes; ZERO effect on output. Report the narrow benefit honestly.
- [ ] **Step 6: Guard + commit (both repos)** — `feat(lever D): pre-index via semantic_search query tool (sole-trigger build, fail-open)`.

---

### Task 9: Final matrix + best_combo + report

**Files:** `runtime/lever-matrix-bench.py` (best_combo auto-union); a results doc.

- [ ] **Step 1: Run the FULL matrix** — `python3 runtime/lever-matrix-bench.py` (baseline → each lever → best_combo). best_combo = auto-union of default-ON flags (Q9). Fixed order + warm-up per config (Q8).
- [ ] **Step 2: Capture the results table** — write `docs/superpowers/plans/2026-06-24-token-reduction-results.md` with the cache%/input/output/cost/quality row per config, ranked by cost reduction, with quality gates.
- [ ] **Step 3: Recommend which levers to promote (default-on)** — based ONLY on the measured table: a lever promotes if it cut cost meaningfully AND held quality. Flag any that regressed (e.g. a byte-stability break, an adoption miss).
- [ ] **Step 4: Commit** — `docs(results): token-reduction matrix — measured per-lever cost/quality + promotion recommendation`.

---

## Self-Review

**Spec coverage:** harness (spec §0) → Task 1; plumbing (spec cross-lever) → Task 2; F/A/C/B/E/D (spec §1, ranked order) → Tasks 3-8; owner decisions Q1-Q10 all bound into Global Constraints or the relevant task step. The byte-stable hazard (C/E/D) → Task 5 Step 1 blocking gate + the Global Constraint. The "C in gateway not shim" invariant → Task 5 + Global. F+A shared classifier → Task 2 (built once).

**Placeholder scan:** every step has the file:line, the command, the expected output, and the code where code is changed. Budgets are measured (READ=512, EDIT=P95×1.2, DEFAULT=2048).

**Type/symbol consistency:** `classify_lane_type` (Task 2) is consumed by F (Task 3) and A (Task 4); `maxOutputTokens` plumbing (Task 2) is the single writer F and A both use; `lane_type` ledger field (Task 1) feeds F/A budgets and G's per-type bucketing. No competing classifiers, no double-writes.

**Order rationale:** harness first (everything is measured), plumbing second (F+A substrate), then ROI order F→A→C→B→E→D, with B+D's fork rebuild batched. Each lever is measured against the baseline before the next.
