# Sub-project A — Engine Stability (run reliably on every workflow)

**Date:** 2026-06-25
**Status:** Design (approved in brainstorming, pending spec review)
**Trigger:** the level-3.1 `audit-sua-pm-branch` workflow: 11/18 lanes timed out at 600s
(lanes read 5-8 files each), and verify-lane timeouts were silently mis-counted as
"rejected" (`why_rejected: None`), so the audit was incomplete AND untrustworthy.

## Hard constraint (from [[reasonix-stability-must-keep-savings]])
Every change here MUST report its effect on output / input / cache and must NOT regress
the proven token savings. Stability is not bought by burning tokens. Default OFF +
measure-then-promote + byte-inert when off applies to every mechanism.

## Root cause (diagnosed from the real workflow JSON, not assumed)
- **Lane-too-big → 600s timeout.** `maxIterPerTurn=50` lets flash loop up to 50 reads;
  each raw file accumulates in the conversation log and is re-sent every iteration
  (bucket-3, measured up to 532K tok). 8 audit lanes each named 5-8 files → 7/8 audit +
  4 verify lanes hit `state=error` (gateway 600s kill). `totalTokens` only 8167 (most
  lanes died before producing real work).
- **Verify-empty counted as rejected.** Script did `verified.filter(v => !v.verdict?.confirmed)`
  → a timed-out verify (null/empty verdict) satisfies `!undefined` → bucketed into
  `rejected`. 4/5 "rejected" findings had `why_rejected: None`. The user's hand-verified
  real issues (missing nav links, edit-draft) were buried this way.

## Three mechanisms (each default OFF, byte-inert when off, token-effect reported)

### A1 — In-lane isolated read (two layers; cut the big lane WITHOUT controller help)

The real fix for lane-too-big is to stop a lane ingesting raw file content. Two layers:

**A1a — Lower the engine outline threshold when a lane is capping (mechanical, no bundle change).**
`engine/run-lane.mjs` calls `buildCodeToolset({ rootDir })` with no `outlineThresholdBytes`,
so it uses the engine default 64 KiB (files ≤64 KiB return FULL content). The engine ALSO
accepts `opts.outlineThresholdBytes` (vendored dist line 43751) and a config path — so the
shim can pass a LOWER threshold (e.g. 24-32 KiB) when a flag is set, with NO change to the
vendored bundle. Effect: a lane reading a 30-50 KiB file gets metadata + head + symbol
outline instead of the full dump → drills in with `range`/`search_content` only where
needed. This is mechanical (the engine enforces it), not advisory-to-flash (the B/C/D
failure mode — flash won't adopt a tool, but it always gets the outline).
- Flag: `REASONIX_LANE_OUTLINE_THRESHOLD_BYTES` (unset → engine default 64 KiB = today's
  behavior, byte-inert). When set, the shim passes it into `buildCodeToolset`.
- **Token effect: REDUCES input** (outline ≪ raw file) and reduces output. Aligned.
- **Quality risk (the real one):** a forced outline can starve a lane of a detail it needed
  → less precise finding. Guard: choose the threshold so only genuinely large files
  outline; small/medium files (most code files) stay full. Measure precision on the audit.

**A1b — `read_file_isolated` already in the engine** (4 refs) — a lane CAN read a big file
in a separate context and get a short summary back. But B-validation showed flash won't
*choose* it (adoption 0/5). So A1b is NOT relied on as the primary fix; A1a (engine-enforced
outline) is. A1b stays available for a lane that explicitly wants it. (No new work for A1b.)

### A2 — Enable OVERSCOPE_REJECT (the second line of defense)
Already built (gateway `overscope_rejection`, default OFF). When on, a lane that names >N
existing files or a bulk-codebase scope is REJECTED with a structured "decompose into
per-file lanes" reply BEFORE it runs 600s. Fail-fast + controller-independent.
- Flag: `CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT` (exists).
- **Token effect: REDUCES** (a rejected lane costs ~0 tokens vs a 600s death). Aligned.
- **Re-measure:** prior A/B showed it's a net-wash on a moderate 12-file lane but the WIN
  is exactly the catastrophic 833-file/532K case — which is the level-3.1 timeout shape.
  Measure on the audit: does it fire on the over-broad audit lanes and cut the timeout?

### A3 — Lane-fail surfaces a clear UNVERIFIED verdict (fix "empty = rejected")
Two parts (mechanism + guidance, per the approved design):
- **Mechanism (engine/gateway):** when a lane times out/errors, instead of letting the
  workflow receive a bare `null` (ambiguous), the result is tagged so a workflow can tell
  "could not verify" from "verified = false". Concretely: the gateway already raises
  `reasonix_timeout`; this surfaces a structured marker the harness/workflow can read.
  (Scope note: the gateway can mark its OWN lane reply; it cannot rewrite a controller's
  JS logic — so A3 is mechanism + the guide below, not a silent auto-fix of every script.)
- **Guidance (PREFIX_GUIDE +1 point):** "a verify lane that returns empty/errored is
  UNVERIFIED, NOT rejected; default to KEEPING the finding (mark it unverified) when a
  verify fails — never drop a real issue on a timeout."
- **Token effect: neutral** (only tagging). Aligned.

## Components & boundaries

| Unit | Responsibility | Layer | Flag (default off) |
|---|---|---|---|
| A1a outline-threshold | shim passes a lower outline threshold so big files summarize | `engine/run-lane.mjs` | `REASONIX_LANE_OUTLINE_THRESHOLD_BYTES` |
| A2 overscope-reject | gateway rejects an over-broad lane fast | `reasonix-native-gateway.py` (built) | `CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT` |
| A3 unverified-marker | lane-fail surfaces UNVERIFIED, not null | gateway + `hooks/reasonix-workflow.py` guide | `CLAUDE_REASONIX_GATEWAY_LANE_FAIL_MARKER` |

## How we'll know it works (real-world test = the vatlieu-kho audit, sub-project B)
After A ships (flags OFF by default, turned ON to measure), re-run the audit workflow on
vatlieu-kho and require: (1) 0 lane timeouts (was 11/18); (2) 0 verify-empty mis-counted as
rejected (every finding is confirmed / unverified, never silently rejected on a timeout);
(3) token/cache NOT regressed (output/input/cache measured on the run, per the hard
constraint); (4) finding precision held (A1a's outline didn't starve lanes of needed
detail — spot-check a few findings against the code). First DELETE the previous workflow's
generated artifacts for objectivity — carefully, list-then-confirm, archive don't rm
(user rule). The audit IS the test workload for A and the deliverable for B.

## Out of scope
- No auto-split-into-N-lanes at the hook level (adversary-proven unbuildable: the JS
  wrapper can't call parallel / would rewrite the controller's await-DAG). A1a (engine
  outline) + A2 (reject) achieve the same "don't ingest too much" outcome differently.
- No vendored-engine bundle edit for A1 (the shim opts route avoids it).
- Sub-project B (the corrected audit) is a separate spec/run after A.
