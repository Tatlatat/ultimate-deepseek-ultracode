#!/usr/bin/env python3
"""Regression for the once-per-session PREFIX_GUIDE injection (commit e75a3b9 B).

The hook injects the full ~2060-token PREFIX_GUIDE on the FIRST workflow of a
session, then a one-line reminder thereafter — keyed by session_id via a marker
file under $TMPDIR/reasonix-workflow-guide. This guards that behavior so it can
not silently regress back to "full guide on every workflow" (which compounds
context growth and reproduces the autocompact thrashing).

These tests would FAIL if the guide-once logic were reverted:
 - test_second_call_same_session_is_reminder_only would see the full guide twice.

Isolated: a fresh temp $TMPDIR per run; never touches real marker state.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "reasonix-workflow.py"

# A minimal Workflow payload: tool_name == "Workflow" + a script with one agent()
# call so rewrite_script finds a lane (count > 0) and the hook emits context.
SCRIPT = (
    "export const meta = { name: 'x', description: 'y' }\n"
    "const a = await agent('audit', {label:'arch', agentType:'reasonix-worker'})\n"
)

# A marker the FULL guide always carries (PREFIX_GUIDE_TEXT) but the one-line
# reminder never does.
FULL_GUIDE_MARKER = "PROMPT-CACHE NOTE"
# A phrase only the one-line reminder carries.
REMINDER_MARKER = "cache guidance was given earlier this session"


def run_hook(tmpdir: str, session_id, env_overrides: dict | None = None) -> dict:
    payload = {"tool_name": "Workflow", "tool_input": {"script": SCRIPT}}
    if session_id is not None:
        payload["session_id"] = session_id
    env = dict(os.environ)
    # Point the marker dir at an isolated temp tree; never touch real /tmp state.
    env["TMPDIR"] = tmpdir
    # Force a deterministic mode + the guide gate ON so the test is independent
    # of the launcher env.
    env["CLAUDE_REASONIX_WORKFLOW_MODE"] = "native"
    env["CLAUDE_REASONIX_WORKFLOW_PREFIX_GUIDE"] = "1"
    if env_overrides:
        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    line = proc.stdout.strip().splitlines()[-1]
    return json.loads(line)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def _ctx(out: dict) -> str:
    return out["hookSpecificOutput"]["additionalContext"]


def test_first_call_in_session_has_full_guide():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(run_hook(tmp, "sess-first"))
        expect(FULL_GUIDE_MARKER in ctx, "first workflow of a session must carry the full guide")
        expect(REMINDER_MARKER not in ctx, "first workflow must NOT be the one-line reminder")


def test_second_call_same_session_is_reminder_only():
    # THE regression: a second workflow under the same session_id must collapse
    # to the one-line reminder, never re-inject the full ~2060-token guide.
    with tempfile.TemporaryDirectory() as tmp:
        first = _ctx(run_hook(tmp, "sess-repeat"))
        expect(FULL_GUIDE_MARKER in first, "first call should warm with the full guide")
        second = _ctx(run_hook(tmp, "sess-repeat"))
        expect(FULL_GUIDE_MARKER not in second,
               "second workflow of the SAME session must NOT re-inject the full guide")
        expect(REMINDER_MARKER in second,
               "second workflow of the same session must carry the one-line reminder")
        # The reminder must be materially smaller than the full guide.
        expect(len(second) < len(first),
               "reminder context must be smaller than the full-guide context")


def test_distinct_sessions_each_get_full_guide():
    # A different session_id is a fresh marker -> warms with the full guide again.
    with tempfile.TemporaryDirectory() as tmp:
        a = _ctx(run_hook(tmp, "sess-A"))
        b = _ctx(run_hook(tmp, "sess-B"))
        expect(FULL_GUIDE_MARKER in a, "session A first call must have the full guide")
        expect(FULL_GUIDE_MARKER in b, "session B (distinct id) must ALSO get the full guide")


def test_no_session_id_fails_open_to_full_guide_every_call():
    # Fail-open: with no session_id the hook can't key a marker, so it must keep
    # the FULL guide on every call (never silently drop the guidance).
    with tempfile.TemporaryDirectory() as tmp:
        first = _ctx(run_hook(tmp, None))
        second = _ctx(run_hook(tmp, None))
        expect(FULL_GUIDE_MARKER in first, "no-session_id first call must have the full guide")
        expect(FULL_GUIDE_MARKER in second,
               "no-session_id must FAIL OPEN: full guide on every call, never the reminder")


def test_guide_gate_off_suppresses_both_forms():
    # When the gate is OFF, neither the full guide nor the reminder appears,
    # regardless of session marker state.
    with tempfile.TemporaryDirectory() as tmp:
        first = _ctx(run_hook(tmp, "sess-off", {"CLAUDE_REASONIX_WORKFLOW_PREFIX_GUIDE": "0"}))
        second = _ctx(run_hook(tmp, "sess-off", {"CLAUDE_REASONIX_WORKFLOW_PREFIX_GUIDE": "0"}))
        for ctx in (first, second):
            expect(FULL_GUIDE_MARKER not in ctx, "gate off must suppress the full guide")
            expect(REMINDER_MARKER not in ctx, "gate off must suppress the reminder too")


if __name__ == "__main__":
    test_first_call_in_session_has_full_guide()
    test_second_call_same_session_is_reminder_only()
    test_distinct_sessions_each_get_full_guide()
    test_no_session_id_fails_open_to_full_guide_every_call()
    test_guide_gate_off_suppresses_both_forms()
    print("PASS: workflow prefix-guide once-per-session")
