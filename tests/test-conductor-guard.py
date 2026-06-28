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

    # FINDING 2: flag ON + Bash mutating + no session_id -> fail-open (allow)
    code, _ = decide({"tool_name": "Bash", "tool_input": {"command": "echo hi > file.txt"}}, env)
    check("fail-open: Bash mutating + no session_id -> allowed", code == 0)

# FINDING 1: non-dict JSON must exit 0 (subprocess test)
import subprocess, json as _json
for bad_input in ["null", "[]", '"hello"', "42"]:
    result = subprocess.run(
        ["python3", os.path.join(ROOT, "hooks", "conductor-guard.py")],
        input=bad_input, capture_output=True, text=True,
        env={**os.environ, "CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY": "1"},
    )
    check(f"fail-open: non-dict JSON {bad_input!r} exits 0", result.returncode == 0)

# --- Task 3: gateway records an escalation to the ledger ---
with tempfile.TemporaryDirectory() as td:
    os.environ["TMPDIR"] = td
    _h.record_escalation("sess-esc", "LANE_ESCALATE: status=exhausted")
    p = _h.escalation_ledger_path("sess-esc")
    check("record_escalation wrote ledger", p and os.path.isfile(p) and os.path.getsize(p) > 0)
    # no-op + no raise on falsy session
    try:
        _h.record_escalation(None, "x"); check("record_escalation(None) no-raise", True)
    except Exception:
        check("record_escalation(None) no-raise", False)

# --- Task 4: FIX F1 — bash_mutates covers cp/mv and the new mutating forms ---
check("bash_mutates: cp a b -> True",   cg.bash_mutates("cp a b") is True)
check("bash_mutates: mv a b -> True",   cg.bash_mutates("mv a b") is True)
check("bash_mutates: dd if=... -> True", cg.bash_mutates("dd if=/dev/zero of=out bs=1M count=1") is True)
check("bash_mutates: truncate -> True", cg.bash_mutates("truncate -s 0 file.txt") is True)
check("bash_mutates: gsed -i -> True",  cg.bash_mutates("gsed -i 's/a/b/' f") is True)
check("bash_mutates: git apply -> True", cg.bash_mutates("git apply patch.diff") is True)
check("bash_mutates: git checkout -- -> True", cg.bash_mutates("git checkout -- src/foo.py") is True)
# fail-open: read/test/grep/git-status must NOT match
check("bash_mutates: git status -> False", cg.bash_mutates("git status") is False)
check("bash_mutates: grep cp /etc -> False", cg.bash_mutates("grep cp /etc/hosts") is False)
check("bash_mutates: pytest -> False", cg.bash_mutates("pytest tests/ -q") is False)
check("bash_mutates: git diff -> False", cg.bash_mutates("git diff HEAD") is False)
check("bash_mutates: git checkout branch -> False (no --)",
      cg.bash_mutates("git checkout main") is False)

# --- Task 5: FIX T2 — writer-discipline: pass -> no ledger, stagnated/exhausted -> ledger ---
# Replicate the gate logic from engine_seam.py:
#   if _hp.get("status") != "pass": record_escalation(session_id, text)
# Test that the ACTUAL harness.record_escalation is NOT called for "pass"
# and IS called for "stagnated"/"exhausted".
from reasonix_gateway.harness import parse_harness_result, record_escalation

with tempfile.TemporaryDirectory() as td:
    os.environ["TMPDIR"] = td

    # Build raw harness texts for each status (format: __HARNESS__:<status>:<attempts>:<lesson>)
    harness_pass      = "__HARNESS__:pass:1:all green"
    harness_stagnated = "__HARNESS__:stagnated:4:no progress"
    harness_exhausted = "__HARNESS__:exhausted:4:budget exceeded"

    # Simulate the engine_seam gate for each status:
    def _simulate_gate(raw_text, session_id):
        """Replicate the engine_seam decision: parse harness result, record only on non-pass."""
        _hp = parse_harness_result(raw_text)
        if _hp is not None and _hp.get("status") != "pass":
            record_escalation(session_id, raw_text)

    _simulate_gate(harness_pass, "sess-pass")
    led_pass = _h.escalation_ledger_path("sess-pass")
    check("writer-discipline: pass -> ledger NOT written",
          led_pass is None or not os.path.isfile(led_pass) or os.path.getsize(led_pass) == 0)

    _simulate_gate(harness_stagnated, "sess-stag")
    led_stag = _h.escalation_ledger_path("sess-stag")
    check("writer-discipline: stagnated -> ledger written",
          led_stag is not None and os.path.isfile(led_stag) and os.path.getsize(led_stag) > 0)

    _simulate_gate(harness_exhausted, "sess-exh")
    led_exh = _h.escalation_ledger_path("sess-exh")
    check("writer-discipline: exhausted -> ledger written",
          led_exh is not None and os.path.isfile(led_exh) and os.path.getsize(led_exh) > 0)

    # Non-harness text (normal lane reply) must NEVER write the ledger
    _simulate_gate("some normal lane output without harness prefix", "sess-normal")
    led_normal = _h.escalation_ledger_path("sess-normal")
    check("writer-discipline: normal text -> ledger NOT written",
          led_normal is None or not os.path.isfile(led_normal) or os.path.getsize(led_normal) == 0)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
