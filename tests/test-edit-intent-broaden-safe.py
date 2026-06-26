#!/usr/bin/env python3
"""_EDIT_INTENT_RE breadth — broaden-safe edit classification.

With READER_BROADEN=1, lanes whose task contains a new edit verb (replace, fix,
optimize, update, change, remove, insert) must classify 'edit', NOT 'read'.
Without broaden the new verbs were previously 'unknown' (not in old regex) and
must stay 'unknown' when the flag is OFF.
"""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)

FLAG = "CLAUDE_REASONIX_GATEWAY_READER_BROADEN"

_P = _F = 0


def chk(cond: bool, msg: str) -> None:
    global _P, _F
    if cond:
        _P += 1
        print(f"  ok   {msg}")
    else:
        _F += 1
        print(f"  FAIL {msg}")


def main() -> int:
    # --------------------------------------------------------------------------
    # 1. BROADEN OFF — new verbs were NOT in the old edit regex, so they were
    #    'unknown' before this fix (verify the baseline).
    # --------------------------------------------------------------------------
    os.environ.pop(FLAG, None)

    # "find and replace" — 'replace' is now in _EDIT_INTENT_RE, so even with
    # broaden OFF the edit path fires first (edit is checked before read/broaden).
    result_off = gw.classify_lane_type(None, "find and replace all foo with bar")
    chk(result_off == "edit",
        f"OFF: 'find and replace all foo with bar' -> edit (replace in edit regex, broaden irrelevant); got {result_off!r}")

    # --------------------------------------------------------------------------
    # 2. BROADEN ON — new edit verbs must classify 'edit', not 'read'.
    #    These are the exact patterns the reviewer flagged as wrongly classifying
    #    'read' when READER_BROADEN is on (broaden reader verbs fire, steal lane).
    # --------------------------------------------------------------------------
    os.environ[FLAG] = "1"

    edit_cases = [
        ("find and replace all foo with bar",       "replace"),
        ("review and replace the deprecated calls", "replace"),
        ("analyze and fix the regression",          "fix"),
        ("audit and remove dead code",              "remove"),
        ("examine and optimize the loop",           "optimize"),
        ("inspect and update the config",           "update"),
        ("study and change the handler",            "change"),
        ("find and remove the dead import",         "remove"),
        ("review and insert a guard",               "insert"),
    ]

    for task, verb in edit_cases:
        got = gw.classify_lane_type(None, task)
        chk(got == "edit",
            f"ON: {task!r} [{verb}] -> edit (not read); got {got!r}")

    # --------------------------------------------------------------------------
    # 3. BROADEN ON — a pure-read lane must still classify 'read' (no regression).
    # --------------------------------------------------------------------------
    pure_read = "analyze the auth module in src/auth.py"
    got_read = gw.classify_lane_type(None, pure_read)
    chk(got_read == "read",
        f"ON: pure-read {pure_read!r} -> read; got {got_read!r}")

    os.environ.pop(FLAG, None)

    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0


if __name__ == "__main__":
    sys.exit(main())
