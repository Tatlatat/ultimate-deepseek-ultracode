# Token-Reduction Experiment — Results & Promotion Recommendation

**Date:** 2026-06-25 (corrected after a full real-DeepSeek validation run)
**Method:** 6 env-flag-gated levers built + measured via `runtime/lever-matrix-bench.py` on real reasonix+DeepSeek. Every lever defaults OFF (measure-then-promote).

## TL;DR (corrected)

- **A (read summary) is the only promoted lever.** Across 8 real-DeepSeek runs it passed every quality gate, dropped read-lane output, and held/raised cache. It is the right lever for both input reduction and the 99% cache target. **Promote: ON.**
- **F (output discipline) was DROPPED after real-DeepSeek validation.** The earlier "−24.9% / promote" call was measured on the SYNTHETIC bench, where the edit lane is a forced StructuredOutput that never exercises F. On the real fan-out (24 runs: 16 F, 8 baseline), F (a) **raised** edit-lane output +43% avg (it makes the model emit a real diff instead of a one-line description — correct, but more output), and (b) nudged the heavy review lane's bad-lane rate to ~1% (3 bad lanes / 16 F runs vs 0 / 8 baseline). Net win unproven → **stays OFF** per measure-then-promote.
- **B, C, D, E were each validated on a dedicated real-world workload (2026-06-25) and ALL stay OFF** — each was run through an adversary-approved test that exercises its REAL trigger (not a stub). Result: only A wins on the real flash fan-out; B/C/D/E have working, byte-safe mechanisms but flash does not realize their benefit. See **"Per-lever real-world validation"** below.
- **Honesty note:** the original promotion of F was an overclaim caused by a bench blind spot (the bench's edit lane can't exercise F). The real-DeepSeek run below is the correction. Nothing was actually turned on in production (the gateway defaults both F and A to OFF; no launcher exported their flags), so this correction lands before any production impact.

See **"Real-DeepSeek correction (2026-06-25)"** below for the full data.

## The final matrix (single-run, real DeepSeek)

```
config            cache_w%  in_tok  out_tok  read_out  edit_out  est_cost  quality
baseline            99.59   290107    4311      1520      2670    784990    PASS
OUTPUT_DISCIPLINE   96.27   291804    6771      1464      5216   1519890    PASS
READ_SUMMARY        99.28   282005    5646      2225      3310    953773    PASS
best_combo          97.06   287462    7389      1989      5193   1456320    PASS
```

**How to read this (important):** `out_tok` total and `est_cost` here are NOT reliable lever signals — they are dominated by the 2 EDIT lanes whose output swings 2670→5216 between configs purely from model non-determinism (the same baseline measured out_tok 4311 / 5533 / 6629 / 4787 across four runs — ±30% variance with nothing changed). `read_out` is the low-variance signal where F/A's cap fires, and across 65 read lanes 0 ever exceeded 512 — the cap works. But the bench's read lanes are already terse, so even `read_out` barely moves here.

## Per-lever measured results (each on its own workload, where the signal is clean)

| Lever | Flag | Measured | Promoted? |
|---|---|---|---|
| **F — output discipline** | `OUTPUT_DISCIPLINE` | SYNTHETIC bench: read-lane cap 0/65 violations. REAL DeepSeek: edit output **+43%** (diff-not-description), review bad-lane rate ~1% (3/16 vs 0/8 baseline) — net win unproven | ❌ **OFF (dropped after real run)** |
| **A — read summary** | `READ_SUMMARY` | read-output **−12.2%**, cache +1pp; real-DeepSeek 8/8 runs pass all gates, cache 99.5%; second-order: read output (512 cap) becomes the next lane's input | **✅ DEFAULT_ON** |
| **C — shared read-cache** | `READ_SUMMARY_CACHE` | byte-stability gate PASS (16/16); C2 only +0.3pts on synthetic (no same-file-reread shape) | ❌ OFF — win unproven on a real re-read-heavy workload |
| **B — sub-agent isolation** | `READ_ISOLATED` | free-choice read-heavy: parent input **−31.2%** (adoption 9/8 lanes); forced-choice fan-out: 0 adoption + slight overhead | ❌ OFF — workload-dependent (opt-in per lane type) |
| **E — speculative prefetch** | `PREFETCH_CONTEXT=advisory` | advisory = zero prompt change (verified); precision 1.0 but weak evidence (1 lane/1 file) | ❌ OFF — advisory measures only; inject not justified yet |
| **D — pre-index** | `PREINDEX` | code shipped + fail-open verified; UNMEASURED (no embedding model pulled) | ❌ OFF — measure when an embed model exists |

## Two real bugs the measurement exposed (fixed)

1. **Classifier poisoning (the headline catch).** F's directive ("NEVER **write**… or **apply**…", "For **edits**:") and the structured-output instruction ("Do NOT **write** sentences like…") contain `_EDIT_INTENT_RE` keywords. The call-site classified the lane on the FULLY ASSEMBLED prompt (task + every injected directive), so EVERY read lane carrying a StructuredOutput tool classified as `edit` → F's 512 read cap never fired (measured: 0/164 lanes ever classified read; 150 became edit). **Fix:** classify on the RAW task text (`lane_task_text(messages)`), before any directive is appended; reword F's directive to carry no edit keyword. Regression test added. After the fix: READ→read, REVIEW→unknown, EDIT→edit, SYNTH→synthesize, and the cap fires correctly.

2. **A's instruction reclassified read→edit** (Task 4): "Do NOT **write** prose" → reworded to "No prose, no narration".

## Real-DeepSeek correction (2026-06-25)

The matrix had only ever run on the synthetic workload, where the EDIT lane is a
forced StructuredOutput (output ~18 tok) that never exercises F. I ran the full
thing end-to-end on real reasonix+DeepSeek and the picture changed.

**Run 1 — full matrix (real DeepSeek):**
```
config            cache_w%  read_out  edit_out  quality
baseline            97.5      1855      4523     PASS
OUTPUT_DISCIPLINE  96.76      1499      5150     FAIL   <- F failed quality
READ_SUMMARY       99.56      2330      6081     PASS
best_combo         96.75      1640      5111     PASS
```

**Run 2 — was F's FAIL a regression or one-shot variance?** Ran baseline+F twice
each, then a 12-run interleaved batch (6 baseline + 6 F) to measure the bad-lane
RATE, not a single point:

| config | clean runs | bad lanes | edit_out vs baseline |
|---|---|---|---|
| baseline | **8 / 8** | **0** | — |
| F | **13 / 16** | **3** (all `review` lane: empty/slow, 73–90s) | **+43% avg** (3807→6410, 4712→9657, 3350→6181, …) |

**Root cause (systematic-debugging, confirmed not guessed):**
1. The bad lanes are always the heavy **review** lane (reads a 16KB shared block, runs in a parallel burst near the 73–90s tail). F appends a directive block → the already-tail-latency review lane crosses the empty/slow edge ~1% of the time. F doesn't break it, it nudges an existing irreducible variance.
2. F's edit-output **increase** is F doing its job, backfiring on this metric: on a free-form edit lane the directive "emit a real MINIMAL diff / SEARCH-REPLACE block" makes the model emit an actual usable diff (with context lines) instead of a one-line "I'll change X" description. A real diff is longer than a description — so output goes UP. (My earlier "−24.9%" was the synthetic edit lane never emitting a diff at all.) Whether that trade is good depends on the workflow: a usable diff saves a downstream round-trip; a description forces one. The bench can't measure that, so F is not promotable on this evidence.

**Decision:** drop F to OFF; keep A ON. Re-promote F only after the bench grows a
FREE-FORM edit lane (not forced StructuredOutput), the edit cap is re-tuned to that
lane's real P95, and F is shown to cut NET output (read saving > edit increase)
without raising the review bad-lane rate.

**Harness lesson (the real one):** a lever that fires on a lane type the bench
stubs out (here: edit via forced StructuredOutput) is UNMEASURED, not measured-zero
— do not promote it. The synthetic bench validated F's read path and its
byte-stability, but its edit path was a blind spot. The fix is a free-form edit lane
+ multi-run bad-lane-RATE (not single-run out_tok, which is edit-variance noise).

## Recommendation

- **Keep A ON (DEFAULT_ON).** It touches the 42.3%-of-cost output bucket on the read
  path, caps output safely, holds/raises cache (99.5% real), and its capped output
  becomes the next lane's smaller input. Proven positive on real DeepSeek.
- **Leave F OFF.** Net win unproven on real fan-out (see correction above). Built,
  byte-safe, available behind its flag for anyone who wants diff-format edit output
  and accepts the cost.
- **Leave C/B/E/D OFF** until measured positive on a representative real workload.

## Two real bugs the measurement exposed (fixed — still valid)

The classifier-poisoning fix and A's reclassification fix (above) remain correct and
necessary regardless of F's promotion — they are in the lane-classification path that
A also depends on.

## Per-lever real-world validation (2026-06-25)

The user required EVERY lever validated on a real workload, not just F+A. An 8-agent
design+adversary workflow first showed that ALL four initial test designs were FAKE
measurements (the F-trap generalized: a lever measured on a lane type that stubs its
trigger is UNMEASURED). After the adversary fixes — including two code additions
(`_input_by_type`/`_input_rows_by_type` in the bench, and a per-process read-trace in
the shim so E's ground truth is ACTUAL reads, not the model's invented `files_read`) —
each lever ran on real reasonix+DeepSeek via `runtime/lever-real-validation.py`.

| Lever | Real-DeepSeek result | Verdict |
|---|---|---|
| **A** read-summary | On 3 verbose free-form read lanes (off leg dumped 25,065 tok of prose), A cut read output to 1,338 (**−94.7%**), quality held (summaries name the right file + carry correct findings, verified on the FULL body), cache rose 72.9→82.3. Cap-only leg (1,536) vs full A (1,338) confirms BOTH layers work (hard 512 cap does most; soft JSON layer adds a bit). | ✅ **ON** — validated end-to-end, both layers |
| **B** read-isolated | Free-choice lane over a 36 KiB file (under the engine's 64 KiB outline threshold, so raw `read_file` returns full content — the case B is meant to prevent). Read-trace over 5 paired runs: **adoption 0/5** — flash NEVER chose `read_file_isolated`, always plain `read_file`. Median parent input ON vs OFF: 22,404 vs 22,116 (**+1.3%**, the extra tool spec). Mechanism sound; flash won't adopt it. | ❌ **OFF** — adoption 0 |
| **C** read-cache | 8 lanes all forced to read the same 134 KiB file (quote-verbatim questions; baseline read verified, ~16K input each). Cache populated + byte-safe. Median input OFF 16,538 vs ON 16,247 (**−1.8%**, noise). Lanes need DIFFERENT specifics from the file, so a shared summary can't replace the re-read. | ❌ **OFF** — re-read not suppressed |
| **D** pre-index | Built a REAL semantic index with `nomic-embed-text` (768-dim, `.reasonix/semantic/index.jsonl` 3.1 MB); `build_preindex` fail-opens correctly (0.1 s, never raises). But flash **never calls `semantic_search`** — even when explicitly invited it called `read_file` on the 134 KiB file 5× and looped. Control with PREINDEX OFF hung identically → the loop/hang is flash behavior, NOT D; D is harmless but unused by flash. | ❌ **OFF** — flash won't use the index |
| **E** prefetch | Precision/recall computed against the shim read-trace (ACTUAL reads, not the model's self-reported `files_read`, which is invented). Pooled **precision 1.00** (3/3 — a predicted path is always really read) but **recall 0.30** (3/10 — the predictor's literal-path regex misses every runtime-discovered file; two no-named-file lanes predicted ∅ but read 3–4 files each). Adversary bar for building inject: precision ≥0.90 AND recall ≥0.60 → recall fails. Inject would prefetch ~30% of needed files while every miss adds dead bytes to the shared prefix → net-negative. | ❌ **OFF** — advisory & inject both not worth it |

**The one-line truth:** on a real flash fan-out, **only A realizes its benefit**. B/C/D/E all have
correct, byte-safe mechanisms, but flash either won't adopt the tool (B, D), or the
workload shape doesn't let the cache/prefetch help (C, E). This is exactly why
measure-then-promote exists — without these real runs, all four would have been
promoted on plausible-but-fake bench numbers, like F was.

**Test infrastructure (kept):** `runtime/lever-real-validation.py` (A|B|C|D|E, each its
adversary-approved workload), `_input_by_type`/`_input_rows_by_type` in the bench, and
the shim `REASONIX_READ_TRACE_DIR` read-trace (off by default, byte-inert — observes the
tool dispatch chokepoint only, never touches specs/prefix). Re-promote any lever only by
re-running its validator and clearing its stated bar.

## What's promoted, what's available

- **On by default:** A (`READ_SUMMARY`).
- **Available, off by default (flip the env flag to use):** F (`OUTPUT_DISCIPLINE`), C (`READ_SUMMARY_CACHE`), B (`READ_ISOLATED`), E (`PREFETCH_CONTEXT=advisory`), D (`PREINDEX`).
- All defaults are byte-identical to pre-change when off — zero cache risk, zero behavior change unless explicitly enabled. (Note: the gateway already defaults every lever's master flag to OFF; "promoted" means recommended-on + in the bench's `DEFAULT_ON_LEVERS`, not a code force-on.)
