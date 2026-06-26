# Sub-project C — Meta-Learning / Anti-Forgetting Design

**Date:** 2026-06-25
**Status:** Design (approved in brainstorming, pending spec review)
**Why first:** This protects the lessons of sub-projects A (engine stability) and B
(vatlieu-kho audit). Without it, hard-won lessons keep getting re-learned each session
— the user's exact complaint: "you did it well before but forgot what you did."

## Problem (diagnosed from the real memory store, not assumed)

The memory mechanism EXISTS and works (48 files + a `MEMORY.md` index auto-loaded each
session — that is why this session remembered F was dropped, A is −94.7%, OVERSCOPE was
built, the env_truthy footgun, etc.). But it has three measured gaps:

1. **Memory is RESULTS, not PROCESS.** Of 48 files: 44 `type: project` (what happened),
   2 `reference`, and only **1 `type: feedback`** (how I should work). The valuable
   *process* lessons are buried inside results files, scattered, easy to miss. Examples
   found buried in the index: "ALWAYS check git status after a workflow — design agents
   must not write code"; "the multi-agent adversarial step caught a regression a solo
   build would have shipped"; "measure cost from prefix-trace not the shared ledger";
   "env_truthy(*names) takes NAMES not a default"; "synthetic bench under-shows (F-trap)".

2. **Index overload.** `MEMORY.md` is 40 lines and grows ~1-2 lines per task. No
   compression/archive threshold; when it grows too large to load fully at session start,
   I begin to forget the older entries — the failure mode itself.

3. **No forced-save.** Saving depends on me remembering to save. If a session ends
   abruptly (e.g. compaction), a lesson can be lost before it is written. There is no
   global CLAUDE.md enforcing "save at the end of every large task."

## Solution — three mechanisms

### C1 — Central `process-lessons.md` (the read-first file)

- A single memory file, `type: feedback`, holding ONLY reusable process lessons +
  footguns — one line each, scannable, not prose.
- **Line format:** `- [RULE|FOOTGUN|PATTERN] <one-sentence lesson> (src: <memory-file>)`
- **Seed content:** extract the process lessons currently scattered across the 48 files.
  Initial set (non-exhaustive — extracted during implementation):
  - `[RULE] check git status after every workflow — design/review agents must not write code (src: reasonix-empty-in-burst-accepted)`
  - `[PATTERN] adversary-review a measurement DESIGN before running it — synthetic benches under-show (the F-trap): a lever measured on a lane type that stubs its trigger is UNMEASURED, not measured-positive (src: reasonix-lever-validation-traps)`
  - `[FOOTGUN] env_truthy(*names, default=) — positional args are env var NAMES, not a default value; pass default= as keyword (src: reasonix-input-cut-orchestrator)`
  - `[FOOTGUN] measure cost from prefix-trace, NOT the shared cost ledger (time-clustering bleeds other sessions' lanes in) (src: reasonix-prefix-guide-AB-kept)`
  - `[RULE] real-world tests must be e2e (HTTP/SSE/spawn/parse), not unit — unit tests passed while real workflows broke (src: reasonix-test-must-be-e2e)`
  - `[PATTERN] root-cause by isolation layer-by-layer (engine vs gateway vs script) before fixing; don't ship a fix that's inert/wrong (src: reasonix-hollow-read-rootcause)`
- **Index position:** the FIRST line of `MEMORY.md`, marked `⭐ READ FIRST`, so each
  session opens this file before anything else.

### C2 — Threshold-based index compression

- **Trigger:** `MEMORY.md` exceeds 45 lines.
- **What is compressed:** ONLY `type: project` entries that are DONE (merged / shipped /
  superseded — e.g. files ending "...done", "BUILT (merged)", "achieved"). NEVER
  compress: `feedback`, `reference`, active `project`, or unresolved root-cause files.
- **How:** group N old done-entries into one `archive-<topic>.md` file + replace their N
  index lines with ONE summary line pointing at the archive. The process lessons inside
  those files were already lifted into C1, so nothing reusable is lost.
- **Safety:** do NOT delete the original files; only merge + shorten the index. (The user
  explicitly warned "don't delete aggressively" — applies here too: archive, never rm.)

### C3 — Forced-save checklist

- **Where:** a fixed section in `process-lessons.md` (C1) + a one-line reminder in a
  feedback memory.
- **Rule:** at the end of every LARGE task (workflow done / sub-project done / bug fix
  done), MANDATORY: (a) what was accomplished, (b) any new process-lesson/footgun → into
  C1, (c) update the index, (d) if index > 45 lines → run C2.
- **The "forcing" function:** since there is no global CLAUDE.md to enforce it, the real
  forcing function is that C1 sits at the top of the index AND contains the checklist —
  so every session reads C1 first and sees the checklist, making it hard to forget.

## Components & boundaries

| Unit | Responsibility | Depends on |
|---|---|---|
| `process-lessons.md` | the read-first list of reusable process lessons + the forced-save checklist | nothing (plain memory file) |
| `MEMORY.md` (index) | one-line pointer per memory; C1 pinned first as ⭐ READ FIRST | the memory files |
| `archive-<topic>.md` | merged done-entries when the index crosses 45 lines | the done project files |

No code, no engine change — this is pure memory-discipline. It is byte-irrelevant to the
token-savings constraint (it touches `~/.claude/projects/.../memory/`, not the fleet).

## How we'll know it works (don't trust unmeasured)

C can't be measured in tokens. Success criteria:
1. **In-session test:** after building C1, the extracted process-lessons must cover the
   footguns I actually hit (git-status, env_truthy, F-trap, prefix-trace, e2e). If C1
   captures them, C1 works.
2. **Cross-session test (the real one):** on the NEXT task (sub-project A), I must read C1
   first and update C1 at the end. If A proceeds WITHOUT me repeating a lesson already in
   C1, that is the evidence C works.

## Out of scope

- No global CLAUDE.md (the user hasn't asked; C1-at-top is the lighter forcing function).
- No automated cron/hook to write memory (saving stays a deliberate step, just enforced
  by the C1 checklist).
- Sub-projects A and B are separate specs (this one is C only).
