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


def run_hook(env_overrides: dict) -> dict:
    env = dict(os.environ)
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
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
    # Force the key absent so the subprocess sees it unset (default → on),
    # regardless of what is in the real environment.
    out = run_hook({"CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE": None})
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
