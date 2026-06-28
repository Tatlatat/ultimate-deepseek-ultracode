# Conductor Mode — Opus as hands-off orchestrator over the Reasonix fleet

**Date:** 2026-06-28
**Status:** Design (brainstorming gate — approved decisions, not yet implemented)
**Topic:** Make Opus structurally behave as a conductor (plan + delegate + review), not an
operator (does the work itself), by removing its operator tools rather than asking it to step back.

---

## 1. Problem

The intended model for claude-reasonix is: **Opus is the conductor / hands-off founder**; the
Reasonix (DeepSeek) worker fleet does the execution. Opus should plan, set acceptance criteria,
dispatch, and review by exception — like Dang Le Nguyen Vu running Trung Nguyen from a mountain
retreat: he plans and checks; the organization executes without his hands.

**Observed failure:** Opus behaves like a small-shop owner — it does most work itself with its own
`Edit`/`Write`/`Bash` tools and uses Reasonix only when convenient. The Reasonix system becomes an
occasional helper, not the default workforce.

**Root cause (from the 8-agent governance research, 2026-06-28):** the harness enforces delegation
for the WRONG tools and merely *requests* it for the RIGHT ones. The two existing PreToolUse hooks
(`only-reasonix-fleet.py` blocking `Agent`/`Task`; `reasonix-workflow.py` rewriting `Workflow`
lanes) make the wrong *way to delegate* mechanically impossible, but leave the operator tools
(`Write`/`Edit`/`MultiEdit`/`Bash`) completely frictionless. The entire "delegate, don't do it
yourself" intent lives ONLY as prose in `system-prompt-reasonix.md`. Org theory confirms:
non-delegation is not a willpower deficit but a rational response to a default where doing-it-
yourself is the locally faster, lower-friction path. The harness IS that default. The system can
guarantee that IF Opus delegates it routes correctly; it cannot make Opus delegate at all.

**The gap the user identified:** an earlier "block only when a Workflow is active" idea fails,
because most of the time there is NO active workflow — the user gives ad-hoc, sequential, un-planned
tasks. Those "small-looking" tasks are often not small, and they are exactly where the small-shop
behavior lives. The block must therefore be **always-on in fleet mode**, not workflow-scoped.

## 2. Governing principle

**Structure, not exhortation.** The owner must be *removed* from the operator role, not asked to
step back. A self-running organization passes a mechanical test: a named execution layer fills the
owner's seat, decision rights are pre-delegated with explicit escalation, and reporting is by-
exception. The redesign makes NOT-delegating the impossible-or-costly path instead of repeatedly
asking Opus to choose the harder path. The design cost is paid ONCE in structure, not re-spent live
on every task by a prompt the model can rationalize around.

## 3. The conductor's irreducible job (what Opus KEEPS)

Opus is not weakened into a no-op; it keeps the genuinely non-delegable owner work:

1. **Plan + decompose every task** — including ad-hoc one-off requests — into independent
   single-owner cells (1 lane = 1 file / function / module / question), then dispatch. Even a
   "small" task is decomposed into a mini-plan and delegated; Opus never does the edit itself.
2. **Write the acceptance criteria / definition-of-done per lane** (the highest-leverage non-
   delegable act). Each lane carries a checkable `acceptanceTest`. No acceptance test ⇒ hollow lane
   ⇒ conductor dragged back to manual edits. (This is also the fix for the measured "hollow lane"
   bug: the harness needs a per-lane acceptance test, not just the flag.)
3. **Allocate budget / set tolerances** — fan-out width, per-lane cost cap, concurrency, and the
   escalation thresholds (which cases legitimately come back up).
4. **Review by exception + handle escalations** — read the exception stream (NOT_ALL_GREEN,
   hollow/empty lanes, failed acceptanceTest, LANE_ESCALATE), not every diff. Green lanes run
   untouched. Synthesize lane outputs; own final architecture/trade-off calls.
5. **Conversational replies to the user** — always Opus, never a lane.

Tools Opus KEEPS: `Read`, `Grep`, `Glob` (to scope for dispatch), Bash for read/test/git, and
conversation. Tools Opus LOSES: `Edit`, `Write`, `MultiEdit`, and clearly-mutating Bash.

## 4. Architecture

Three components.

### 4.1 `conductor-guard.py` — the core PreToolUse hook

A new hook wired into `bridge-settings.json` as a `PreToolUse` matcher on
`Edit|Write|MultiEdit|Bash`.

- **Default action: DENY (exit 2)** for `Edit`/`Write`/`MultiEdit`, and for `Bash` commands that
  clearly mutate files. The denial message redirects: *"You are the conductor. Decompose this into
  lane(s) with an acceptanceTest and dispatch via `mcp__reasonix_fleet__run_reasonix_worker` (or an
  `agent()` lane in a Workflow). Do not edit files yourself."*
- **Always-on in fleet mode** — NOT scoped to a workflow marker. The hook fires for every
  qualifying tool call whenever the conductor guard is enabled. This is what closes the
  user-identified gap (ad-hoc, no-workflow work).
- **Bash classification:** deny Bash whose command matches clearly-mutating patterns —
  output redirection (`>`, `>>`), `sed -i`, `tee`, `cat <<EOF >`/here-doc-to-file, and in-place
  `cp`/`mv` that overwrite a tracked file. Allow all other Bash (reads, `git`, test runs, `ls`,
  `grep`, build/scope commands). When the classifier is uncertain whether a Bash command writes,
  it **allows** (fail-open — never block a test run on a guess).
- Opus's other tools (`Read`/`Grep`/`Glob`) are never touched.

### 4.2 Escalation safety valve

The guard is strict but must never trap the user.

- The gateway / Reasonix MCP writes an **escalation ledger** entry when a lane returns
  `LANE_ESCALATE`, fails its `acceptanceTest`, or comes back hollow/empty. Entry is keyed by
  session id.
- `conductor-guard.py` reads the ledger. If there is an **unresolved escalation for this session**,
  it ALLOWS `Edit`/`Write` (Opus may now fix the broken lane's work by hand — the legitimate
  exception, exactly the CEO-intervenes-on-incident model).
- **Fail-OPEN everywhere:** if the ledger is unreadable, missing, or detection is ambiguous, the
  guard ALLOWS the edit. The guard never wedges the user. A blocked edit is only ever the result of
  a confident "no escalation pending + clearly an operator action."

### 4.3 Enable / measure

- Default **OFF**, behind `CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY=1` (set by the launcher only when
  the user opts in). `0`/unset ⇒ the guard is a no-op (byte-inert) and Opus edits as today.
- A/B before promoting to default (per the project's measure-then-promote rule): measure
  inline-edit count (target → ~0 for non-exception turns), lanes dispatched, hollow rate, total cost
  and cache. Do not claim "now it delegates" without these numbers.

## 5. Prompt cleanup (replace exhortation with structure)

Once the guard exists, the prose policy must change so it does not contradict or undercut the
structure:

- **Delete** the per-edit small-edit carve-out (`system-prompt-reasonix.md:31-32` "one genuinely
  small edit inline is fine", line 49) — it is the loophole that lets a multi-step task be
  rationalized as N small edits. The structure, not the model's private self-classification,
  decides.
- **Move** the "833-file lane is worse" passage (lines 70-72) out of the orchestrator-facing prompt
  into the lane-decomposition guide only. As written it is a policy-blessed argument to operate
  instead of orchestrate; it should inform HOW to decompose, never WHETHER to delegate.
- **Drop** the "Banned excuses" list as enforcement (keep at most one line). The hook is the
  enforcement; a banned-words list enforces nothing against a model that just does the work.
- **Keep** "your DEFAULT is to delegate" — but only because the guard now actually backs it.

## 6. Risks and guards

| Risk | Guard |
|---|---|
| Blocking a genuinely-tiny real edit (false-positive, same class as the past "overscope rejected EVERY lane" bug) | Escalation valve + fail-open + default-OFF + A/B before promote |
| Opus can never fix a stalled/hollow lane | Escalation ledger mints edit-permission on LANE_ESCALATE / failed acceptanceTest / empty result; fail-open if detection uncertain |
| Escalation / dispatch ping-pong (Opus blocked → dispatches an under-decomposed lane → it can't form → loop) | Opus's irreducible job #1 (fine decomposition) + per-lane acceptanceTest; pair the block with the decomposition guide |
| Opus evades the block via Bash file-writes | §4.1 denies clearly-mutating Bash patterns (`>`, `>>`, `sed -i`, `tee`, here-doc-to-file) while allowing reads/tests/git |
| Over-bluntness (killing legit one-liners session-wide) | Implemented as a hook gated by the opt-in env flag, not a launcher-wide `--disallowedTools`; fail-open and escalation valve keep it from trapping |
| "It now delegates" claimed without measurement | no-claim-without-measurement: A/B with the harness, default OFF until measured, `git status` after any workflow so a design agent does not silently ship the hook enabled |

## 7. Scope boundary (YAGNI)

In scope: the always-on `conductor-guard.py` hook (Edit/Write/MultiEdit + mutating-Bash), the
escalation-ledger read + fail-open valve, the launcher opt-in flag, the prompt cleanup, and the A/B
measurement.

Explicitly OUT of scope for this spec (deferred — they were lower-ranked research options): the
inline-edit counting circuit-breaker (mechanism #3), the post-turn accountability reporter
(mechanism #4), and any change to how lanes themselves run. The hard block (mechanism #1) is the
load-bearing change; the others are catch-all/measurement layers to consider only if the block
proves insufficient after measurement.

## 8. Success criteria

- With the flag ON, on an ad-hoc "do X" request (no workflow), Opus decomposes and dispatches to
  Reasonix instead of editing files itself; measured inline-edit count on non-exception turns ≈ 0.
- A lane that escalates/fails unlocks Opus's edit tools for that session (the valve works).
- With the flag OFF, behavior is byte-identical to today (the guard is inert).
- The full test suite stays green, including a new regression test that the guard denies an
  Edit when enabled+no-escalation and allows it when disabled or when an escalation is pending.
