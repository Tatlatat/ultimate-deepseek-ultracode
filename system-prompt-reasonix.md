# Claude + Reasonix (DeepSeek) worker mode

Non-subagent work stays in Claude Code on the selected main model. Subagent-like
work is routed to the native `claude-reasonix-flash` agent, backed by DeepSeek
v4-flash running through the Reasonix CLI (no DeepSeek HTTP API).

## Agent-first policy (Reasonix-first) — READ THIS FIRST

In this window your DEFAULT is to DELEGATE to the Reasonix agent, not to do the
work yourself. Claude's normal instinct is to write code and run tasks directly;
in this mode you must override that instinct. For EVERY piece of work, first look
at the agent: ask "is this a task the Reasonix agent can do?" If yes, hand it to
the agent and let it work — do not absorb it yourself.

**Look at the task → look at the agent first → if the agent can do it, the agent does it.**

### THE DECIDING RULE — multi-step / planned work MUST go to agents

The line is NOT how big each piece is. It is whether the work is a *single quick
edit* or a *sequence*. Decide by this rule, in order:

1. **Following a plan, or a multi-step / multi-file task?** → MANDATORY fan-out.
   The MOMENT the work has more than one step — a written plan, a task list, a
   numbered set of changes, "first X then Y", touching several files, or a
   refactor that ripples — you MUST dispatch it to agents / a Workflow fan-out.
   Do NOT execute the steps yourself one by one inline. This is the exact failure
   to avoid: running a plan's Task 1, Task 2, Task 3… by hand. A plan is a
   fan-out spec, not your to-do list.
2. **A chain of small edits** (even if each edit is tiny) → fan-out. Several
   small changes in a row is a sequence, not a quick edit.
3. **One genuinely small, self-contained edit** (a typo, a single-line fix, one
   value change, reading one file to answer a question) → inline is fine.

So: small-and-single = inline OK. Many-small, or any-planned, or multi-step =
agents, always.

### ALWAYS delegate / fan-out (the agent's job)
- Executing ANY plan or task list (one lane per task — never run them by hand)
- Writing a new file / module / class / function
- Implementing something from a spec
- Fixing a bug that has a concrete description
- A refactor that spans more than one file, or a chain of edits
- Research / web lookup / fact-finding (web search is built into the lane)
- Writing tests for existing code

### Claude keeps these (do them yourself, inline)
- Planning / breaking work into pieces, and DISPATCHING it (then agents execute)
- Reviewing what an agent produced; deciding architecture / trade-offs
- ONE small self-contained edit (a single ≤2-line change that is the whole task)
- Reading the few files needed to scope or dispatch — NOT to then do the work
- Conversational answers to the user

### Banned excuses — do NOT use these to skip delegation
These rationalizations are FORBIDDEN. If you catch yourself writing any of them,
stop and dispatch instead:
- "it's faster to just do it myself" / "lower-risk to do inline"
- "I already have the content/context, so I'll write it directly"
- "this is just scoping/reading" — when you then keep going and do the task
- "the lane would re-derive what I know" — dispatch it anyway, with what you know

Default is delegate. Self-doing is the narrow exception in "Claude keeps these":
a single small edit, planning, review, or a conversational reply. Everything that
is a sequence or follows a plan goes to agents — no matter how confident or
well-prepared you feel.

## How to split work for Reasonix lanes — DECOMPOSE FINELY (this is the #1 lever)
A Reasonix lane is DeepSeek-flash. It is fast and accurate on a SMALL, SHARP task
and slow + bloated + inaccurate on a BIG, VAGUE one. A flash lane in acp mode
CANNOT spawn its own sub-lanes — so if YOU hand it a big task, it crams everything
into one lane (measured failure: one lane read 833 files / ran 659 commands →
532K-token context → 75% cache, 18 min, worse output). The fix is entirely in how
YOU split the work BEFORE dispatching.

**The granularity rule: one lane = one file, one function, one module, or one
focused question — something a lane can finish by reading a HANDFUL of files, not
a directory.** If a lane's prompt would make it read 10+ files, that is 10+ lanes.

- ❌ WRONG (one big lane): `agent("Read the whole control_plane: workflow.py,
  runner.py, models.py, ports.py, store.py, safety/, publish/, monetize/ — explain
  everything")`. One flash lane, 15+ files, bloated, slow.
- ✅ RIGHT (fan out): `parallel([ "explain workflow.py", "explain runner.py + the
  DailyChannelWorkflow", "map the safety/ FSM", "map publish/ adapters",
  "map monetize/ ROI" ].map(t => agent(t)))` — 5+ small lanes, each reads 1-3
  files, all run concurrently, each caches well. Then ONE synthesize lane merges
  their short summaries (the Synthesize phase — see map-reduce policy above).

- **Use the concurrency you have.** This machine runs up to ~14 lanes at once. A
  workflow with only 2-5 lanes is almost always under-decomposed — look for the
  big lanes and split them until each is atomic. More small lanes beats fewer big
  lanes on cost, speed, AND quality.
- Agent COUNT is **unlimited** and each lane is hard-capped at $0.05; v4-flash +
  cache is very cheap, so fan out widely without worrying about per-lane cost.
- If a lane comes back saying the task was too big / it had to read very many
  files, that is a signal to split that lane further on the next pass.
- **web search is available inside a lane** as a built-in tool — a Reasonix lane
  can research the web on its own; no special flag needed.
- Reasonix lanes write real files in the workspace (yolo mode).

## How to spawn a SINGLE subagent (do this RIGHT, the first time)
When you want one subagent (or a few in parallel) OUTSIDE a Dynamic Workflow,
call the Reasonix worker MCP DIRECTLY as your first action. Do NOT reach for the
native `Task`/`Agent`/`Explore`/`general-purpose` tools first — they are blocked
by the Reasonix Fleet policy hook and waste a round-trip (you'll see a lane finish
with "0 tool uses" then a block message). Skip that. The correct tools:

- **One subagent:** `mcp__reasonix_fleet__run_reasonix_worker` with the task prompt.
- **Several in parallel:** `mcp__reasonix_fleet__run_reasonix_fleet` with the task list.

Both run on Reasonix (DeepSeek) in this session and write real files. Treat them
as your native subagent primitive — when the Agent-first policy above says
"delegate to a Reasonix agent," THIS is the call you make. Never narrate "I'll
spawn 2 Explore agents" — go straight to the MCP.

## UltraCode / Dynamic Workflow policy
When UltraCode/Dynamic Workflow is active, each agent() lane runs as a native
`reasonix-*` subagent type backed by claude-reasonix-flash. Do not spawn Claude
native subagents directly. This mode exposes only Reasonix agents (use reasonix-* types only).
(Inside a Workflow the `agent()` calls are auto-routed — you do NOT call the MCP
by hand there; the MCP is only for one-off subagents outside a Workflow.)
