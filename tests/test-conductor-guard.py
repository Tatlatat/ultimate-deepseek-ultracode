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

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
