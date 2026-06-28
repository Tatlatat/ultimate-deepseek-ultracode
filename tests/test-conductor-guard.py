#!/usr/bin/env python3
"""Unit tests for conductor-guard hook + the shared escalation-ledger path."""
import importlib.util
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

passed = 0
failed = 0
def check(label, cond):
    global passed, failed
    if cond:
        print(f"  ok   {label}"); passed += 1
    else:
        print(f"  FAIL {label}"); failed += 1

# --- Task 1: ledger path helper (defined in reasonix_gateway/harness.py) ---
sys.path.insert(0, ROOT)
from reasonix_gateway import harness as _h

with tempfile.TemporaryDirectory() as td:
    os.environ["TMPDIR"] = td
    p = _h.escalation_ledger_path("sess-abc")
    check("ledger path includes session id", p is not None and p.endswith("sess-abc"))
    check("ledger path under tmpdir", p is not None and p.startswith(td))
    check("ledger path in conductor-escalations dir", "reasonix-conductor-escalations" in p)
    check("None session -> None path", _h.escalation_ledger_path(None) is None)
    check("empty session -> None path", _h.escalation_ledger_path("") is None)

# --- Task 2: the hook decision logic ---
spec = importlib.util.spec_from_file_location(
    "conductor_guard", os.path.join(ROOT, "hooks", "conductor-guard.py"))
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)

def decide(payload, env):
    saved = dict(os.environ)
    os.environ.update(env)
    try:
        return cg.decide(payload)
    finally:
        os.environ.clear(); os.environ.update(saved)

ON = {"CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY": "1"}

# flag OFF -> always allow (byte-inert default)
code, _ = decide({"tool_name": "Edit", "tool_input": {"file_path": "x.py"}, "session_id": "s1"}, {})
check("flag OFF: Edit allowed", code == 0)

# flag ON, no escalation -> deny Edit/Write/MultiEdit
with tempfile.TemporaryDirectory() as td:
    env = {**ON, "TMPDIR": td}
    for tool in ("Edit", "Write", "MultiEdit"):
        code, msg = decide({"tool_name": tool, "tool_input": {"file_path": "x.py"}, "session_id": "s1"}, env)
        check(f"flag ON, no escalation: {tool} denied", code == 2 and "conductor" in msg.lower())

    # Read/Grep/Glob always allowed
    for tool in ("Read", "Grep", "Glob"):
        code, _ = decide({"tool_name": tool, "tool_input": {"file_path": "x.py"}, "session_id": "s1"}, env)
        check(f"flag ON: {tool} allowed", code == 0)

    # Bash mutating -> deny ; Bash read -> allow
    code, _ = decide({"tool_name": "Bash", "tool_input": {"command": "echo hi > file.txt"}, "session_id": "s1"}, env)
    check("flag ON: Bash redirect-write denied", code == 2)
    code, _ = decide({"tool_name": "Bash", "tool_input": {"command": "sed -i 's/a/b/' f"}, "session_id": "s1"}, env)
    check("flag ON: Bash sed -i denied", code == 2)
    code, _ = decide({"tool_name": "Bash", "tool_input": {"command": "pytest tests/ -q"}, "session_id": "s1"}, env)
    check("flag ON: Bash test-run allowed", code == 0)
    code, _ = decide({"tool_name": "Bash", "tool_input": {"command": "git status && grep foo *.py"}, "session_id": "s1"}, env)
    check("flag ON: Bash git/grep allowed", code == 0)

    # escalation valve: ledger file present+nonempty -> allow Edit
    led_dir = os.path.join(td, "reasonix-conductor-escalations")
    os.makedirs(led_dir, exist_ok=True)
    with open(os.path.join(led_dir, "s1"), "w") as f:
        f.write("LANE_ESCALATE: status=stagnated\n")
    code, _ = decide({"tool_name": "Edit", "tool_input": {"file_path": "x.py"}, "session_id": "s1"}, env)
    check("escalation pending: Edit allowed (valve)", code == 0)

    # fail-open: no session_id -> allow
    code, _ = decide({"tool_name": "Edit", "tool_input": {"file_path": "x.py"}}, env)
    check("fail-open: no session_id -> allowed", code == 0)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
