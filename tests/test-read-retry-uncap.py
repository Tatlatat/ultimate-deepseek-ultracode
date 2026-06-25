#!/usr/bin/env python3
"""Lever A truncation recovery: an A-capped read lane over a large file spends its
512-token cap on tool-calls/reasoning/outline and gets truncated BEFORE emitting the
answer -> the engine returns empty text (measured: ~50% at cap 512, ~0% at no cap).
Fix: when an A-capped read lane (retry_empty_force) returns empty, retry ONCE at a
higher cap (default 2x) so the model has budget to finish. Verified: recovers 2/2.

We can't drive DeepSeek here, so we test the cap-escalation DECISION via the pure
helper retry_cap_for_empty(): returns the escalated cap (or None = no retry).
"""
import importlib.util, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(gw)

_P = _F = 0
def chk(c, m):
    global _P, _F
    if c: _P += 1; print(f"  ok   {m}")
    else: _F += 1; print(f"  FAIL {m}")


def main():
    for k in ("CLAUDE_REASONIX_GATEWAY_READ_RETRY_HOLLOW",
              "CLAUDE_REASONIX_GATEWAY_READ_RETRY_CAP_MULT"):
        os.environ.pop(k, None)

    # force off => no escalation regardless of emptiness
    chk(gw.retry_cap_for_empty(orig_cap=512, was_empty=True, force=False) is None,
        "force off: no retry cap")
    # force on, but result NOT empty => no retry
    chk(gw.retry_cap_for_empty(orig_cap=512, was_empty=False, force=True) is None,
        "force on, non-empty: no retry")
    # force on, empty, capped => escalate to 2x (default)
    chk(gw.retry_cap_for_empty(orig_cap=512, was_empty=True, force=True) == 1024,
        "force on, empty, cap 512 -> 1024 (2x default)")
    chk(gw.retry_cap_for_empty(orig_cap=1024, was_empty=True, force=True) == 2048,
        "force on, empty, cap 1024 -> 2048")
    # no original cap (None) => nothing to escalate (truncation can't be the cause)
    chk(gw.retry_cap_for_empty(orig_cap=None, was_empty=True, force=True) is None,
        "no original cap: no retry (truncation not the cause)")
    # env override of the multiplier
    os.environ["CLAUDE_REASONIX_GATEWAY_READ_RETRY_CAP_MULT"] = "3"
    chk(gw.retry_cap_for_empty(orig_cap=512, was_empty=True, force=True) == 1536,
        "multiplier env override 3x -> 1536")
    os.environ.pop("CLAUDE_REASONIX_GATEWAY_READ_RETRY_CAP_MULT", None)

    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0


if __name__ == "__main__":
    sys.exit(main())
