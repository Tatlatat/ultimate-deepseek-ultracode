#!/usr/bin/env python3
"""Broadened read classifier — flag off = identical to today; flag on = read-heavy
verbs classify 'read' WITHOUT stealing synthesis/edit lanes."""
import importlib.util, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(gw)
FLAG = "CLAUDE_REASONIX_GATEWAY_READER_BROADEN"
_P = _F = 0
def chk(c, m):
    global _P, _F
    if c: _P += 1; print(f"  ok   {m}")
    else: _F += 1; print(f"  FAIL {m}")
def main():
    os.environ.pop(FLAG, None)
    # OFF: today's behavior — these verbs are 'unknown'
    for v in ("analyze", "review", "audit", "find", "examine", "inspect", "summarize"):
        chk(gw.classify_lane_type(None, f"{v} the auth module") == "unknown",
            f"OFF: '{v} the auth module' -> unknown (unchanged)")
    chk(gw.classify_lane_type(None, "read the file src/x.py") == "read", "OFF: literal read still read")
    # ON: read-heavy verbs now classify 'read'
    os.environ[FLAG] = "1"
    for v in ("analyze", "review", "audit", "find", "examine", "inspect", "investigate", "study", "trace", "explain"):
        chk(gw.classify_lane_type(None, f"{v} the auth module in src/auth.py") == "read",
            f"ON: '{v} ...' -> read (Lever A now reaches it)")
    # ON: synthesis-intent STILL wins (no theft)
    chk(gw.classify_lane_type(None, "review and merge the findings into one report") == "synthesize",
        "ON: 'review and merge' stays synthesize")
    chk(gw.classify_lane_type(None, "examine all findings and consolidate across sources") == "synthesize",
        "ON: 'examine ... consolidate across sources' stays synthesize")
    # ON: edit-intent STILL wins
    chk(gw.classify_lane_type(None, "review and then refactor the module") == "edit",
        "ON: 'review and refactor' stays edit")
    os.environ.pop(FLAG, None)
    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0
if __name__ == "__main__":
    sys.exit(main())
