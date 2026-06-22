# Workflow Prefix-Cache Guide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inject a short, env-gated advisory guide into the Workflow PreToolUse hook's `additionalContext` that tells the model to assemble each lane's prompt prefix-stable, then A/B-measure whether it reduces cold-start cache misses and keep it only if it does.

**Architecture:** Add a `PREFIX_GUIDE_TEXT` module constant to `hooks/codex-workflow.py` and append it to the already-built `additional_context` (right after the `selfheal_context` append at line 372-373) when `CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE` is on (default on). The guide fires only when the Workflow tool is invoked. The existing opt-in `prefix-trace.jsonl` instrument is used to A/B the guide; the guide is kept only if measured to help.

**Tech Stack:** Python 3 (`hooks/codex-workflow.py`). Test via subprocess driving the hook with a Workflow payload on stdin and reading `additionalContext` from the stdout JSON. No new dependencies.

## Global Constraints

- Spec of record: `docs/superpowers/specs/2026-06-22-workflow-prefix-cache-guide-design.md`. Every task implicitly includes its requirements.
- The guide is ADVISORY — its text ends with "This is advisory — correctness first; apply where it doesn't distort the work." Never reword that out.
- Env gate: `CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE`, default `"1"` (on). Off for any of `0/false/no/off` (case-insensitive). Read fresh each invocation.
- The guide fires ONLY in the Workflow PreToolUse path, after `additional_context` is built and `selfheal_context` is appended (codex-workflow.py ~L372-373). It is appended to `additional_context`, never replaces the mode text or selfheal text.
- No change to the gateway, launcher, lane logic, or the fleet/native/router mode branches. Only an append + a module constant.
- `os` is already imported at the top of `codex-workflow.py` (line 3) — do not re-import.
- Keep/drop is binary and measured (Task 3): keep ONLY if guide-ON weighted-cache rises ≥2% over guide-OFF AND cost/lane drops on the same task. Otherwise revert the guide.
- Working directory: `~/.claude/codex-fleet` (git-tracked; base commit before this plan: `4caf420`). Run tests with `python3 tests/<name>.py`.

---

## File Structure

- `hooks/codex-workflow.py` — MODIFY: add `PREFIX_GUIDE_TEXT` constant near the other module constants (after `MARKER` on line 14); add the env-gated append after line 373.
- `tests/test-workflow-prefix-guide.py` — CREATE: subprocess-drives the hook with a Workflow payload, asserts the guide is present when the env is on and absent when off, and that mode + selfheal context are untouched.

---

## Task 1: Add the guide constant and the env-gated append

**Files:**
- Modify: `hooks/codex-workflow.py` (constant after line 14 `MARKER = ...`; append after line 373 `additional_context = additional_context + "\n\n" + selfheal_context`)
- Test: `tests/test-workflow-prefix-guide.py`

**Interfaces:**
- Produces: module constant `PREFIX_GUIDE_TEXT: str`; the hook's emitted `additionalContext` ends with `PREFIX_GUIDE_TEXT` iff `CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE` is on.

- [ ] **Step 1: Record the base commit**

Run: `cd ~/.claude/codex-fleet && git rev-parse --short HEAD`
Expected: `4caf420` (the review-package base for this task).

- [ ] **Step 2: Write the failing test**

Create `tests/test-workflow-prefix-guide.py`. It drives the hook as a subprocess with a Workflow tool payload on stdin (the same shape the Claude Code PreToolUse hook receives) and inspects the `additionalContext` in the emitted JSON. Use the repo's `expect()` style (raise SystemExit on failure) and print a PASS line at the end so it matches the other suites.

```python
#!/usr/bin/env python3
"""Tests the env-gated prefix-cache guide in codex-workflow.py's Workflow hook."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "codex-workflow.py"

# A minimal Workflow tool payload: the hook only needs tool_name == "Workflow"
# and a tool_input.script containing at least one agent() call so rewrite_script
# finds a lane (count > 0) and the hook emits additionalContext.
SCRIPT = (
    "export const meta = { name: 'x', description: 'y' }\n"
    "const a = await agent('audit', {label:'arch', agentType:'codex-worker'})\n"
)
PAYLOAD = {
    "tool_name": "Workflow",
    "tool_input": {"script": SCRIPT},
}


def run_hook(env_overrides: dict[str, str]) -> dict:
    env = dict(os.environ)
    env.update(env_overrides)
    # Force a deterministic mode so the test is independent of the launcher env.
    env.setdefault("CLAUDE_CODEX_WORKFLOW_MODE", "native")
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(PAYLOAD),
        capture_output=True,
        text=True,
        env=env,
    )
    # The hook prints one JSON object on stdout for a handled Workflow call.
    line = proc.stdout.strip().splitlines()[-1]
    return json.loads(line)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def _ctx(out: dict) -> str:
    return out["hookSpecificOutput"]["additionalContext"]


def test_guide_present_when_on():
    out = run_hook({"CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE": "1"})
    ctx = _ctx(out)
    expect("PROMPT-CACHE NOTE" in ctx, "guide must be present when gate is on")
    expect("Per-lane data scope" in ctx, "guide rule 1 must be present")
    expect("advisory — correctness first" in ctx, "guide must keep the advisory disclaimer")


def test_guide_absent_when_off():
    for val in ("0", "false", "no", "off", "OFF"):
        out = run_hook({"CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE": val})
        ctx = _ctx(out)
        expect("PROMPT-CACHE NOTE" not in ctx, f"guide must be absent when gate={val!r}")


def test_guide_default_on():
    env = dict(os.environ)
    env.pop("CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE", None)
    out = run_hook({})  # gate unset → default on
    expect("PROMPT-CACHE NOTE" in _ctx(out), "guide must default to on when env unset")


def test_mode_and_structure_preserved():
    # With the guide on, the native-mode context text must still be present and
    # the guide must be APPENDED (mode text first, guide after).
    out = run_hook({"CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE": "1"})
    ctx = _ctx(out)
    expect("native Claude Code subagents" in ctx, "native mode context must survive")
    expect(ctx.index("native Claude Code subagents") < ctx.index("PROMPT-CACHE NOTE"),
           "guide must be appended AFTER the mode context, not replace it")


if __name__ == "__main__":
    test_guide_present_when_on()
    test_guide_absent_when_off()
    test_guide_default_on()
    test_mode_and_structure_preserved()
    print("PASS: workflow prefix-cache guide")
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd ~/.claude/codex-fleet && python3 tests/test-workflow-prefix-guide.py`
Expected: FAIL — `PROMPT-CACHE NOTE` is not in the context yet (the guide constant + append don't exist).

- [ ] **Step 4: Add the `PREFIX_GUIDE_TEXT` constant**

In `hooks/codex-workflow.py`, after the `MARKER = "__codexWorkflowAgent"` line (line 14), add:
```python
PREFIX_GUIDE_TEXT = (
    "PROMPT-CACHE NOTE for this Dynamic Workflow: each agent() lane runs on\n"
    "DeepSeek via reasonix, where a cache MISS costs ~50x a hit. To keep lanes\n"
    "cheap, assemble each lane's prompt prefix-stable:\n"
    "1. Per-lane data scope: give a lane ONLY the data it needs (e.g. a verify\n"
    "   lane gets the ONE finding it checks, not the whole findings set). Smaller\n"
    "   unique payload = fewer missed tokens.\n"
    "2. Shared-first ordering: put content COMMON across same-role lanes (the\n"
    "   source file they all read, a fixed instruction template) at the START of\n"
    "   the lane prompt; put the lane-specific task/data LAST.\n"
    "3. Batch by shared data: when several lanes consume the same data set, give\n"
    "   them the same set in the same order so they share a cached prefix.\n"
    "This is advisory — correctness first; apply where it doesn't distort the work."
)
```

- [ ] **Step 5: Add the env-gated append**

In `hooks/codex-workflow.py`, immediately after the existing block (line 372-373):
```python
    if selfheal_context:
        additional_context = additional_context + "\n\n" + selfheal_context
```
add:
```python
    if os.getenv("CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE", "1").lower() in {"1", "true", "yes", "on"}:
        additional_context = additional_context + "\n\n" + PREFIX_GUIDE_TEXT
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd ~/.claude/codex-fleet && python3 tests/test-workflow-prefix-guide.py`
Expected: `PASS: workflow prefix-cache guide`.

- [ ] **Step 7: Run the existing workflow suite (no regression)**

Run: `cd ~/.claude/codex-fleet && python3 tests/test-workflow-selfheal.py`
Expected: `PASS: workflow self-heal preflight + wrapper sentinel` (the change only appends text; existing behavior unchanged).

- [ ] **Step 8: Commit**

```bash
cd ~/.claude/codex-fleet
git add hooks/codex-workflow.py tests/test-workflow-prefix-guide.py
git commit -m "feat(workflow): env-gated prefix-cache guide in Workflow additionalContext"
```

---

## Task 2: Verify the guide fires in all modes and the suite is green

**Files:**
- Test only: `tests/test-workflow-prefix-guide.py` (extend), plus a full reasonix suite run.

**Interfaces:**
- Consumes: `PREFIX_GUIDE_TEXT` + the env gate from Task 1.

- [ ] **Step 1: Add a mode-coverage test case**

Append to `tests/test-workflow-prefix-guide.py`, before the `__main__` block, a test that the guide appends in each mode (the guide must be mode-agnostic — it appends after whichever mode text was built):
```python
def test_guide_appends_in_each_mode():
    for mode, marker in (
        ("fleet", "Codex Fleet"),
        ("router", "Claude Code Router routes"),
        ("native", "native Claude Code subagents"),
    ):
        out = run_hook({
            "CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE": "1",
            "CLAUDE_CODEX_WORKFLOW_MODE": mode,
        })
        ctx = _ctx(out)
        expect(marker in ctx, f"{mode} mode context text must be present")
        expect("PROMPT-CACHE NOTE" in ctx, f"guide must append in {mode} mode")
        expect(ctx.index(marker) < ctx.index("PROMPT-CACHE NOTE"),
               f"guide must come after the {mode} mode text")
```
and call `test_guide_appends_in_each_mode()` in the `__main__` block before the `print(...)`.

- [ ] **Step 2: Run the guide test**

Run: `cd ~/.claude/codex-fleet && python3 tests/test-workflow-prefix-guide.py`
Expected: `PASS: workflow prefix-cache guide`.

- [ ] **Step 3: Run the full reasonix unit suite (no regression anywhere)**

Run:
```bash
cd ~/.claude/codex-fleet
python3 tests/test-reasonix-acp.py
python3 tests/test-mcp-reasonix.py
python3 tests/test-reasonix-cost-ledger.py
python3 tests/test-workflow-selfheal.py
python3 tests/test-workflow-prefix-guide.py
```
Expected: each prints its PASS line.

- [ ] **Step 4: Commit**

```bash
cd ~/.claude/codex-fleet
git add tests/test-workflow-prefix-guide.py
git commit -m "test(workflow): guide appends in fleet/router/native modes"
```

---

## Task 3: A/B measurement and the keep/drop decision

This task is an EXPERIMENT, not code. It produces a decision (keep or revert) plus a recorded result. It is run by the controller, not a subagent — the implementer subagent has no tmux/session access. The controller drives the live runs; the subagent path for this plan ends at Task 2.

**Files:** none modified by default. (If the decision is "revert", a follow-up commit removes the guide — see Step 6.)

- [ ] **Step 1: Pick a fixed task T and a fixed codebase**

Choose one repeatable UltraCode task that fans out many review/verify lanes (the cold-start case), e.g. `ultracode review and harden <module>` against a fixed repo at a fixed commit. Record T verbatim so both runs use the identical prompt.

- [ ] **Step 2: Baseline run (guide OFF)**

In a fresh `claude-reasonix` session started with:
```bash
export CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE=0
export CLAUDE_CODEX_GATEWAY_PREFIX_TRACE=1
```
First clear the trace: `: > ~/.claude/codex-fleet/runtime/prefix-trace.jsonl`. Run task T to completion. Then compute the token-weighted cache and cost/lane:
```bash
python3 - <<'PY'
import json
rows=[json.loads(l) for l in open('/Users/tatlatat/.claude/codex-fleet/runtime/prefix-trace.jsonl')]
hit=miss=0
for r in rows:
    inp=r.get('in_tok') or 0; c=r.get('cache_pct')
    if c is not None and inp: hit+=inp*c/100; miss+=inp*(1-c/100)
print("lanes", len(rows), "weighted_cache %.2f%%" % (100*hit/(hit+miss) if hit+miss else 0))
PY
```
Record: lane count, weighted cache, and the cost-ledger delta for the run.

- [ ] **Step 3: Treatment run (guide ON)**

Restart a fresh session with `CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE=1` (default) and `CLAUDE_CODEX_GATEWAY_PREFIX_TRACE=1`, clear the trace again, run the SAME task T, compute the same metrics.

- [ ] **Step 4: Compare and decide**

Keep the guide iff: weighted-cache(ON) − weighted-cache(OFF) ≥ 2 percentage points AND cost/lane(ON) < cost/lane(OFF). If a single pair is ambiguous (within noise), run a second pair before deciding; UltraCode is dynamic so expect run-to-run variance.

- [ ] **Step 5: Record the result**

Write the measured numbers (both runs, the delta, the decision) to memory (`reasonix-cache-coldstart-measured` or a new note) so the decision is durable and re-derivable.

- [ ] **Step 6: If the decision is REVERT**

```bash
cd ~/.claude/codex-fleet
git revert --no-edit <Task-1-commit-sha>   # removes PREFIX_GUIDE_TEXT + the append
# keep tests/test-workflow-prefix-guide.py only if it still passes with the guide gone;
# otherwise git rm it. The prefix-trace instrument stays (it was committed earlier in 4caf420).
git commit  # if the revert needs a follow-up for the test file
```
If the decision is KEEP, no further action — the guide is on by default and the runtime off-switch (`CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE=0`) remains available.

---

## Self-Review (completed by plan author)

**Spec coverage:** guide constant + append → Task 1 ✅; env gate default-on + off-values → Task 1 test + impl ✅; advisory disclaimer kept → Task 1 constant + test asserts it ✅; fires only in Workflow path after selfheal append → Task 1 Step 5 anchor (after L373) ✅; appended not replacing mode/selfheal → Task 1 `test_mode_and_structure_preserved` + Task 2 per-mode ✅; no gateway/launcher/lane change → Global Constraints + only file touched is codex-workflow.py ✅; A/B same-task-twice + ≥2% threshold + binary keep/drop + revert path → Task 3 ✅; weighted cache (token-weighted) formula → Task 3 Step 2 ✅; instrument reused (already committed 4caf420) → Task 3 uses it, not re-added ✅.

**Placeholder scan:** none — the guide text is given verbatim, the wiring is the exact 2-line append, the test file is complete, and the A/B commands are concrete. Task 3 is intentionally an experiment with exact commands rather than code.

**Type/name consistency:** `PREFIX_GUIDE_TEXT` and `CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE` are spelled identically in the constant, the append, every test, and Task 3. The env on-set `{"1","true","yes","on"}` matches the spec's stated off-values (anything else = off, with default `"1"`). The test marker strings (`"native Claude Code subagents"`, `"Codex Fleet"`, `"Claude Code Router routes"`) are copied from the real mode-text branches at codex-workflow.py L353/L362/L369.
