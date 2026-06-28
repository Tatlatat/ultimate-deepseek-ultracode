# Silent-Worker output-style — make the reasonix session talk minimally

**Date:** 2026-06-28
**Status:** Design (brainstorming gate — approved decisions, not yet implemented)
**Topic:** Make the Opus orchestrator in a claude-reasonix session talk minimally (Fable-5 style:
run a long chain of tools in silence, speak only when it truly matters) — by Claude Code's
built-in output-style mechanism, not by stuffing the system prompt with "be concise".

---

## 1. Problem

In a claude-reasonix session, Opus narrates constantly: roughly "1-2 tools → a paragraph → 1-2
tools → another paragraph". The user rarely reads these paragraphs and they cost output tokens.
Fable 5 (the user's reference) does the opposite — it runs 15-20 tools in a row saying nothing,
and speaks only once or twice, only when something genuinely matters.

Three narration types the user wants gone (confirmed by the user + by analysing a real reasonix
transcript, 2026-06-28):
1. **Reasoning narrated to screen** — "This is a single edit, so I'll...", "Per the policy this is a fan-out...".
2. **Pre/post-tool narration** — "I'll read X", "Let me first scope", "OK, done reading, next...".
3. **Long explanations after a result** — a 1500-char "here's why each is safe" block when the user only needed the verdict.

## 2. Why this is the right lever (and why NOT a hook)

Hard technical fact (verified 2026-06-28): **Claude Code hooks cannot rewrite the assistant's
text output.** PreToolUse blocks/allows tool calls; PostToolUse runs after a tool; Stop fires when
the turn ends — none can edit the prose Opus already generated. So "a hook that trims chatter" is
not feasible; it is dropped from the design.

The tokens are spent when the model GENERATES text, not when it is displayed. Reducing chatter
therefore can only be done by changing the model's behavioral instruction. The strongest,
correct-by-design way Claude Code offers is an **output-style**: a markdown file that REPLACES the
default "how you respond" part of the system prompt (vs `--append-system-prompt`, which only tacks
a sentence on the end that the model readily ignores mid-task — the same way it ignored the
delegation policy). Output-style is the mechanism Claude Code itself uses for concise/explanatory
modes; it is not a prompt hack.

This is an honest correction of the user's "don't fix it via prompt" framing: there is no
non-instruction path to make Opus generate less — the generation happens inside the model. The
distinction that matters is append-prompt (weak, ignored) vs output-style (deep, behavior-replacing).

## 3. The boundary — what to CUT vs what to KEEP (the core of this design)

Derived from a real reasonix transcript's text blocks (the conductor-loop vscode-test session):

**CUT (do not generate):**
1. Pre/post-tool narration ("I'll read X", "Let me scope", "Now I'll…", "done, next…").
2. Decision reasoning narrated to screen ("this is a single edit so…", "per the policy…").
3. Long post-result explanation / analysis / tables when the user asked only for the result.
4. In a long tool chain (15-20 tools): **stay completely silent between tools.**

**KEEP (always say):**
1. The final RESULT line — short, with concrete data: "Done. Added `// X` at `src/main.ts:6`." /
   "29 occurrences, none need fixing."
2. A genuinely-needed question for the user's decision (AskUserQuestion).
3. A warning / surprise / risk: a real bug found, an irreversible action, a failed lane,
   an unexpected blocker.
4. A direct explanation when the user explicitly asked for one (explain on request, never
   volunteer).

The litmus test the style encodes: **"Would the user act on this sentence, or skip it?"** If skip
→ don't generate it. Result, decision-needed, and warning are act-on-able → keep. Narration and
volunteered reasoning are skippable → cut.

## 4. Architecture

- **`output-styles/silent-worker.md`** — new file shipped in the fleet. Defines the minimal-talk
  behavior with the §3 boundary. It is an output-style document (replaces the response-behavior
  section), NOT an appended sentence.
- **`bridge-settings.json`** — add `"outputStyle": "silent-worker"` so the reasonix session
  activates it. (The setting names the style; Claude Code loads the matching file from the
  install's output-styles dir.)
- **`bin/claude-reasonix`** — ensure the output-styles dir is on the path Claude Code reads for the
  reasonix session, and honor an OFF switch `CLAUDE_REASONIX_SILENT=0` (default ON in reasonix
  flavor; setting it to 0 removes the outputStyle from the rendered settings so behavior reverts).
- **`install.sh`** — copy `output-styles/*.md` into the install home (like it copies hooks).

Default: **ON in every reasonix session** (the user wants the reasonix session to always talk
minimally), with `CLAUDE_REASONIX_SILENT=0` as the escape hatch. The plain `claude` session and
other flavors are unaffected.

## 5. Measurement (no-claim-without-measurement)

A/B in the isolated sandbox loop (the conductor-loop rig already built): run the same set of tasks
once WITHOUT the output-style and once WITH it. Measure, from the session transcript:
- number of assistant text blocks (chatter count),
- total assistant output tokens,
- AND confirm the KEEP set still appears: the final result line, any warnings, and any
  decision-questions are NOT suppressed.

Promote only if chatter/tokens drop substantially AND no KEEP-class message was lost.

## 6. Risks

| Risk | Guard |
|---|---|
| Over-silence — a needed result/warning/lane-failure gets suppressed | §3 KEEP list is explicit (4 mandatory-speak classes); A/B verifies they still appear |
| Opus ignores the output-style mid-task (as it ignored the delegation policy) | output-style replaces behavior (deeper than append-prompt); A/B measures real effect, not assumed |
| A long silent chain hides a stall from the user | KEEP-class "failed lane / unexpected blocker" must still surface; the user also watches via /rc |
| Style bleeds into non-reasonix sessions | scoped to the reasonix rendered settings only; plain claude untouched |

## 7. Scope (YAGNI)

In scope: the `silent-worker.md` output-style, the settings wiring, the launcher OFF-switch, the
install copy, and the A/B measurement. OUT of scope: any hook-based trimming (not feasible), any
change to what tools Opus uses, and per-task verbosity tuning. One style, on by default, measured.

## 8. Success criteria

- With the style ON, on a long task Opus runs its tool chain with no per-tool narration and emits
  only: the final result line, any warning, any decision-question.
- Measured chatter-count and output-tokens drop substantially vs OFF, with zero KEEP-class loss.
- With `CLAUDE_REASONIX_SILENT=0`, behavior reverts to today (the style is not applied).
- Plain `claude` sessions are byte-identical to today.
