#!/usr/bin/env python3
"""Unit tests for the big-read-guard hook: it stops the orchestrator from reading a
huge file whole into its own context (the measured autocompact-thrashing cause).
Range reads pass; small files pass; fail-open on uncertainty; default ON."""
import importlib.util
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("brg", os.path.join(ROOT, "hooks", "big-read-guard.py"))
brg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(brg)

passed = 0
failed = 0
def check(label, cond):
    global passed, failed
    if cond:
        print(f"  ok   {label}"); passed += 1
    else:
        print(f"  FAIL {label}"); failed += 1

def decide(payload, env=None):
    saved = dict(os.environ)
    if env:
        os.environ.update(env)
    try:
        return brg.decide(payload)
    finally:
        os.environ.clear(); os.environ.update(saved)

with tempfile.TemporaryDirectory() as td:
    big = os.path.join(td, "big.ts")
    small = os.path.join(td, "small.ts")
    with open(big, "w") as f:
        f.write("x" * 200000)   # 200KB > 50KB default threshold
    with open(small, "w") as f:
        f.write("x" * 1000)     # 1KB

    # whole read of a big file -> BLOCK (exit 2)
    code, msg = decide({"tool_name": "Read", "tool_input": {"file_path": big}})
    check("whole read of big file -> deny", code == 2 and "BIG READ" in msg)

    # range read of a big file -> ALLOW (offset/limit is the correct way)
    code, _ = decide({"tool_name": "Read", "tool_input": {"file_path": big, "limit": 60}})
    check("range read (limit) of big file -> allow", code == 0)
    code, _ = decide({"tool_name": "Read", "tool_input": {"file_path": big, "offset": 100}})
    check("range read (offset) of big file -> allow", code == 0)

    # small file -> ALLOW
    code, _ = decide({"tool_name": "Read", "tool_input": {"file_path": small}})
    check("small file whole read -> allow", code == 0)

    # relative path resolved against payload cwd -> BLOCK (the bug that was fixed)
    code, _ = decide({"tool_name": "Read", "tool_input": {"file_path": "big.ts"}, "cwd": td})
    check("relative big path + cwd -> deny", code == 2)

    # relative path WITHOUT cwd -> fail-open ALLOW (can't resolve)
    code, _ = decide({"tool_name": "Read", "tool_input": {"file_path": "big.ts"}})
    check("relative big path, no cwd -> fail-open allow", code == 0)

    # non-Read tool -> ALLOW
    code, _ = decide({"tool_name": "Edit", "tool_input": {"file_path": big}})
    check("non-Read tool -> allow", code == 0)

    # missing file_path -> fail-open ALLOW
    code, _ = decide({"tool_name": "Read", "tool_input": {}})
    check("missing file_path -> fail-open allow", code == 0)

    # nonexistent file -> fail-open ALLOW
    code, _ = decide({"tool_name": "Read", "tool_input": {"file_path": os.path.join(td, "nope.ts")}})
    check("nonexistent file -> fail-open allow", code == 0)

    # guard OFF -> ALLOW even a big whole read
    code, _ = decide({"tool_name": "Read", "tool_input": {"file_path": big}},
                     {"CLAUDE_REASONIX_BIG_READ_GUARD": "0"})
    check("guard OFF -> allow big read", code == 0)

    # custom threshold: lower it so the small file now trips
    code, _ = decide({"tool_name": "Read", "tool_input": {"file_path": small}},
                     {"CLAUDE_REASONIX_BIG_READ_THRESHOLD_BYTES": "500"})
    check("custom low threshold -> small file now denied", code == 2)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
