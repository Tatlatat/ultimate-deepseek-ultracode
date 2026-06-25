#!/usr/bin/env python3
"""C3: parse the shim's __HARNESS__ text into a SHORT structured result; a stagnated/
exhausted lane is marked ESCALATE so Opus reviews only failures, never raw files."""
import importlib.util, sys
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
    # non-harness text -> None (passthrough, byte-inert)
    chk(gw.parse_harness_result("just a normal lane reply") is None, "normal text -> None")
    # pass
    p = gw.parse_harness_result("__HARNESS__:pass:2:")
    chk(p == {"status": "pass", "attempts": 2, "lesson": ""}, "parses pass")
    r = gw.harness_lane_reply(p)
    chk("pass" in r and "ESCALATE" not in r, "pass reply has no ESCALATE")
    # stagnated -> ESCALATE marker for Opus
    p = gw.parse_harness_result("__HARNESS__:stagnated:3:error at x.ts:42")
    chk(p["status"] == "stagnated", "parses stagnated")
    r = gw.harness_lane_reply(p)
    chk("ESCALATE" in r and "x.ts:42" in r, "stagnated reply carries ESCALATE + the lesson")
    # exhausted -> ESCALATE
    chk("ESCALATE" in gw.harness_lane_reply(gw.parse_harness_result("__HARNESS__:exhausted:4:")), "exhausted -> ESCALATE")
    # the reply is SHORT (the whole point — no raw files)
    chk(len(gw.harness_lane_reply(gw.parse_harness_result("__HARNESS__:pass:1:"))) < 200, "reply is short")
    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0
if __name__ == "__main__":
    sys.exit(main())
