# Workflow Prefix-Cache Guide — Design

**Goal:** Reduce UltraCode/Dynamic-Workflow cold-start cache misses (measured weighted ~95%, target higher) by guiding how the workflow assembles each lane's prompt so same-role lanes share a longer cached prefix — and KEEP the guide only if an A/B measurement proves it helps.

**Why:** Measured root cause (see memory `reasonix-cache-coldstart-measured`): cold lanes are review/verify lanes that receive a large per-lane findings JSON (13-15K tokens) in their prompt. After a shared ~8K head (Claude core + the source file they all read) the prompt diverges into each lane's own findings, which DeepSeek has never seen → a miss. A cache miss costs ~50× a hit ($0.14 vs $0.0028/Mtok), so at scale this matters: measured $0.0497 extra per ~79-lane audit = 6.2% of that run's cost, ~$50 over 1000 such runs. The gateway cannot reorder (it sees each lane independently, with no cross-lane context); the only controllable lever is HOW the workflow divides data across lanes, which we can influence via an advisory guide injected when the Workflow tool fires.

**Architecture:** One component plus one measurement mechanism.
- **Guide:** `hooks/codex-workflow.py` already injects an `additional_context` string into the session via the PreToolUse(Workflow) hook (it currently appends `selfheal_context`). We append a short, env-gated `PREFIX_GUIDE_TEXT` that advises prefix-stable lane-prompt assembly. It fires only when the Workflow tool is invoked, so it never bloats ordinary prompts.
- **Measurement:** the existing opt-in `prefix-trace.jsonl` instrument (gated by `CLAUDE_CODEX_GATEWAY_PREFIX_TRACE`) is used to A/B the guide. This is a hypothesis-with-verification fix: the guide is kept ONLY if measured to help.

**Tech Stack:** Python 3 (`hooks/codex-workflow.py`). No new dependencies.

## Global Constraints

- The guide is ADVISORY, not enforced. Its text ends with "correctness first; apply where it doesn't distort the work" so the model never bends the task to chase cache.
- The guide is opt-out-able at runtime via `CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE` (default `"1"` = on; any of 0/false/no/off = off). Read fresh each invocation so A/B needs no code change.
- The guide fires ONLY on the Workflow PreToolUse hook (not per lane, not per ordinary turn). Its cost is ~140 tokens per workflow ≈ $0.0004 at miss price — negligible vs the $0.05 cold-start it targets.
- No change to the gateway, launcher, lane logic, or the existing fleet/native/router mode branches. The guide only APPENDS text to the already-built `additional_context`.
- Keep/drop decision is binary and measured: keep ONLY if guide-ON weighted-cache rises ≥2% over guide-OFF AND cost/lane drops, on the same task. Otherwise revert the guide (keep the instrument + the diagnosis in memory).

---

## Component — the guide

**File:** `hooks/codex-workflow.py`. Add a module constant `PREFIX_GUIDE_TEXT` and append it to `additional_context` (after the `selfheal_context` append, ~L373) when the env gate is on.

**`PREFIX_GUIDE_TEXT` (≈140 tokens):**
```
PROMPT-CACHE NOTE for this Dynamic Workflow: each agent() lane runs on
DeepSeek via reasonix, where a cache MISS costs ~50× a hit. To keep lanes
cheap, assemble each lane's prompt prefix-stable:
1. Per-lane data scope: give a lane ONLY the data it needs (e.g. a verify
   lane gets the ONE finding it checks, not the whole findings set). Smaller
   unique payload = fewer missed tokens.
2. Shared-first ordering: put content COMMON across same-role lanes (the
   source file they all read, a fixed instruction template) at the START of
   the lane prompt; put the lane-specific task/data LAST.
3. Batch by shared data: when several lanes consume the same data set, give
   them the same set in the same order so they share a cached prefix.
This is advisory — correctness first; apply where it doesn't distort the work.
```

**Each rule maps to measured data:**
- Rule 1 ↔ cold lanes received the WHOLE findings set (13K) instead of one finding. Scoping the unique payload down to ~1-2K is the biggest lever.
- Rule 2 ↔ the shared prefix is only ~8K (the source file) before divergence; shared-first lengthens it.
- Rule 3 ↔ same-family lanes do not share a 32k prefix (they diverge before 32k); same data in same order extends the shared region.

**Wiring (the only code change):**
```python
# after: additional_context = additional_context + "\n\n" + selfheal_context
if os.getenv("CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE", "1").lower() in {"1", "true", "yes", "on"}:
    additional_context = additional_context + "\n\n" + PREFIX_GUIDE_TEXT
```

---

## Measurement mechanism (A/B — the decision gate)

Same task, run twice, one variable changed:
1. **Baseline (guide OFF):** `export CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE=0 CLAUDE_CODEX_GATEWAY_PREFIX_TRACE=1`; run task T; record weighted-cache and cost/lane from `prefix-trace.jsonl` + the cost ledger.
2. **Treatment (guide ON):** `CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE=1` (default) with `PREFIX_TRACE=1`; run the SAME task T; record again.
3. Compare weighted-cache and cost/lane.

**A/B fairness:**
- Same task T, same codebase, same gateway code; only the guide flag differs.
- Clear `prefix-trace.jsonl` between runs so the two datasets are separate.
- Because UltraCode is dynamic (the model writes a different workflow script each run), there is run-to-run noise. Use a task T large enough (many lanes) that the signal beats noise, and run 2-3 pairs if a single pair is ambiguous.

**Weighted cache** = `sum(in_tok * cache_pct) / sum(in_tok)` over the run's lanes (token-weighted, not the per-lane average — a few huge cold lanes dominate cost).

---

## Error handling (fail-safe)

- The guide is a string append; there is no runtime failure path. If the env read somehow fails, the default is `"1"` (on).
- If `PREFIX_GUIDE_TEXT` were empty, `additional_context` is still valid — only the guide is missing, the workflow is not broken.
- Because the guide is advisory and explicitly says "correctness first", even a misreading cannot make the model distort the work to chase cache.

## Testing

- **Unit** (extend `tests/test-workflow-selfheal.py` or add a focused test): with `CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE=1`, the hook's `additionalContext` CONTAINS `PREFIX_GUIDE_TEXT`; with `=0`, it does NOT. Assert the gate works in all three modes (fleet/native/router) and the existing context (mode text + selfheal) is unchanged.
- **No regression:** `python3 tests/test-workflow-selfheal.py` and the reasonix unit suite still pass (the change only appends text).
- **A/B experiment (decision gate, not a unit test):** the same-task-twice procedure above. This decides keep vs revert.

**Success metric (binary, measured):** on the same task, guide-ON weighted-cache rises ≥2% over guide-OFF AND cost/lane drops → KEEP. Otherwise → REVERT the guide (text + wiring); keep the prefix-trace instrument (opt-in) and the diagnosis in memory.

## Rollback

- The guide is one module constant + one `if` block in `hooks/codex-workflow.py` (git-tracked). Drop = revert the commit, or set `CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE=0` (runtime off, no code change).
- The prefix-trace instrument was committed separately (opt-in, inert unless enabled) — keep it regardless of the guide's fate.

## Non-Goals (YAGNI)

- Gateway-side prefix reordering — proven infeasible (no cross-lane context).
- Changing the launcher, lane logic, or the fleet/native/router mode branches.
- Enforcing how the workflow divides data — the guide is advisory only.
- Guaranteeing 99% — the lever is the model honoring the guide, which is outside infrastructure control; the design accepts that the guide may prove ineffective and is dropped if the A/B says so.
