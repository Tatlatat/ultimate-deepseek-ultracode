# Weak-Executor Harness — Strong-Plan / Weak-Execute / Cheap-Review

**Date:** 2026-06-25
**Status:** Design (approved in brainstorming, pending spec review)
**Goal:** Make DeepSeek-v4-flash actually COMPLETE hard agentic coding work in isolated lanes (instead of returning vacuous results that force the Opus orchestrator to do it by hand), while keeping the system CHEAPER than plain Opus — fixing the measured 19M-token / 97%-cache-read paradox.

## The measured problem (the whole reason for this)
On a HARD task (split a 1497-line class, keep 157 tests green), un-scaffolded DeepSeek fan-out lanes FAILED (0 files, vacuous "tests pass"); the Opus orchestrator then did it by hand across 122 turns = **19M tokens, 97% of which was cache-read** from an accumulating session context that compacted twice ≈ **$42** — more than plain Opus. See [[reasonix-orchestrator-cost-paradox]].

## Evidence base (measured, cited — see [[reasonix-weak-executor-harness-research]])
- **xRouter (Salesforce 2025):** orchestration does NOT emerge in weak models — they need explicit scaffolding + instance specs. (This IS our failure.)
- **SWE-agent (2405.15793):** a purpose-built coding harness lifts the SAME model ~+10.7pp (~2×). Biggest single lever.
- **Reflexion (2303.11366):** run→fail-test→write a short self-critique→retry takes HumanEval pass@1 80%→91% (+11pp). BUT: "inference cost grows ~quadratically with accumulated trial history" — naive retry RE-CREATES our cache-read paradox.
- **COPE (2506.11578):** strong-planner / weak-executor (1-2 sentence plan) lifts a small model to BEAT GPT-4o on MBPP at ~75% lower cost; the reverse DEGRADES.
- **CMU EMNLP 2025 (2505.20182):** INSTANCE-LEVEL per-task plans help; coarse repo-dumps, summaries, few-shot HURT.
- **FrugalGPT/RouteLLM:** cascade with a reliability score accepts most weak outputs, escalates only low-confidence → ~95% of GPT-4 quality at a fraction of cost.

## Hard constraint
The harness MUST be CHEAPER than plain Opus, not more expensive. Every component reports its token/cost effect. The retry loop must NOT re-create the quadratic cost blowup. Default OFF + measure-then-promote + byte-inert when off (the system-wide rule, [[reasonix-stability-must-keep-savings]]).

## Architecture — 3 components

### Component 1 — Strong-plan / forced fan-out (orchestrator side)
**Where:** `hooks/reasonix-workflow.py` PREFIX_GUIDE_TEXT (advisory injected into the Workflow call).
**What:** add guidance that, for a HARD task, the Opus orchestrator MUST (a) decompose into lanes where **each lane is a COMPLETE sub-task** (not just "draft" — it includes edit + verify), and (b) hand each lane an **instance-level spec**: a 1-2 sentence plan + the EXACT files it touches + an acceptance test/command. It must NOT dump repo structure, summaries, or few-shot examples into the lane (measured to HURT).
**Token effect:** REDUCES — a precise short spec is far smaller than a repo dump; it also prevents the orchestrator from doing the work by hand (the 19M case).
**Note:** this is advisory (the orchestrator is Opus, capable of following it) — the mechanical enforcement of "complete the work" lives in Component 2 (the lane harness) + Component 3 (escalation only on real failure).

### Component 2 — Lane harness: a PROGRESSING, non-bloating, bounded retry loop (engine side)
**Where:** `engine/run-lane.mjs` — the `CacheFirstLoop` construction (currently `maxIterPerTurn: req.maxIterPerTurn ?? 1`, no acceptance-test loop). Uses existing engine primitives: `budgetUsd` (constructor option), `compactHistory` (engine method, already auto-folds), `maxIterPerTurn`.
**What:** when a lane's request carries an `acceptanceTest` (a shell command, e.g. `bun test X`) and the harness flag is on, the lane runs an EXECUTE → RUN-TEST → (if fail) REFLECT → RETRY loop with THREE guards that make it progress AND stay cheap:

- **(a) Lesson-only carry (chống cost bình phương):** after a failed attempt, the lane keeps ONLY a SHORT lesson ("error X at file:line because Z → next try W"), NOT the raw attempt history. The next attempt's context = the instance spec + the CURRENT code on disk (re-read fresh, small) + the short lesson. Drive this via the engine's `compactHistory` so accumulated turns are folded, not appended — this is the explicit fix for Reflexion's quadratic-cost warning.
- **(b) Progress-gate (chống lặp vô ích):** measure a progress signal between attempts — primarily the acceptance-test FAILURE COUNT (did failing tests decrease?), secondarily whether the error is DIFFERENT from last time (not the same line/message). If an attempt shows NO progress (failure count didn't drop AND the error is the same as the previous attempt), STOP immediately and mark the lane for escalation (Component 3) — do NOT keep retrying. This is the precise mechanism that replaces a blunt iteration cap: stop on stagnation, not on a fixed count.
- **(c) Budget cap (lưới an toàn rẻ):** pass `budgetUsd` (a small per-lane USD cap, e.g. $0.05) so a lane can never run away cost-wise; on hitting it, stop + escalate. Guarantees the harness is cheaper, never more expensive.

The loop ends when: tests pass (success) OR no-progress (stagnation → escalate) OR budget hit (→ escalate). The lane returns a SHORT structured result (Component 3), never raw history.
**Token effect:** the loop only re-runs while making measured progress, on a folded (non-growing) context, under a hard $ cap — so it spends MORE only when it is converging, and stops early when it isn't. This is why it is safe to RE-ENABLE the loop the user previously disabled (which spun uselessly with no stop condition).

### Component 3 — Cheap review on short results / escalate-only-on-failure (orchestrator side)
**Where:** the lane RETURN shape (`engine/run-lane.mjs` output + gateway lane reply) + Lever A's read-summary substrate (`reasonix-native-gateway.py`).
**What:** a harness lane returns a SHORT structured result — `{status: pass|stagnated|budget, diff_stat, files_changed, test_result, lesson_if_failed, one_line_summary}` — NOT the raw file contents or the raw conversation. The Opus orchestrator REVIEWS those short results (cascade-style): accept lanes that passed; ESCALATE (Opus does it itself) ONLY the lanes that stagnated/hit budget. Opus never re-reads raw files for a passing lane — this is the direct fix for the 97% cache-read blowup.
**Token effect:** REDUCES dramatically — the orchestrator's per-lane input drops from "raw file re-read every turn" to "a few-hundred-token structured result," and Opus only spends real tokens on the minority of lanes that genuinely failed.

## How it fixes the paradox (end to end)
- Opus orchestrator: plans (short instance specs) + reviews (short structured results) + escalates only failures → NO accumulating raw-file context → the 97% cache-read disappears.
- DeepSeek lanes: actually complete hard work via the harness (ACI tools + progressing retry) in their OWN small context → no vacuous results.
- Net: DeepSeek does the heavy lifting it failed at before; Opus is a cheap planner+reviewer; cost goes DOWN, not up.

## Components & boundaries

| Unit | Responsibility | Where | Flag (default off) |
|---|---|---|---|
| C1 strong-plan guide | force complete-subtask fan-out + instance-level specs | `hooks/reasonix-workflow.py` PREFIX_GUIDE | reuse `CLAUDE_REASONIX_WORKFLOW_PREFIX_GUIDE` (+ a sub-point) |
| C2 lane harness | progressing/non-bloating/bounded retry on acceptanceTest | `engine/run-lane.mjs` (CacheFirstLoop opts) | `REASONIX_LANE_HARNESS` |
| C3 short result + escalation | structured short lane result; Opus reviews/escalates | lane return + gateway + Lever A | `REASONIX_LANE_RESULT_STRUCTURED` |

## How we'll know it works (measure — the project's iron rule)
Re-run the HARD Bun task (split the 1497-line class) through reasonix WITH the harness on, and measure vs the $42/19M baseline:
1. **Did DeepSeek lanes actually complete it?** (files created, refactor real, 157 tests green) — the thing they failed at zero-shot.
2. **Total cost INCLUDING the Opus orchestrator session** (orchestrator .jsonl message usage incl cache_read + lane ledger) — must be FAR below $42, ideally a small multiple of the DeepSeek lane cost. (Never report lane-only cost again — that was the hidden-cost mistake.)
3. **Cache-read %** of the orchestrator — must collapse from 97% (no raw-file re-read).
4. **Loop health:** lanes that retried actually showed decreasing test-failures (progress), and stagnating lanes stopped early (no useless spinning).
5. **Quality held:** the refactor is correct (verbatim moves, no escape corruption) — independent blind review.

## Out of scope
- Not making DeepSeek the ORCHESTRATOR (harness keeps Opus as planner/reviewer — the user's quality requirement).
- Not training/fine-tuning anything (pure prompt/harness, API-implementable per the research).
- Not a general SWE-bench agent rebuild — this is a harness around the EXISTING engine lane, reusing buildCodeToolset (the ACI tools) + compactHistory + budgetUsd.
