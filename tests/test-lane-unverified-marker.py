#!/usr/bin/env python3
"""A3: a lane that times out/errors surfaces a LANE_UNVERIFIED marker (not a bare null),
so a workflow never mis-counts a verify timeout as a 'rejected' finding."""
import importlib.util, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(gw)
FLAG = "CLAUDE_REASONIX_GATEWAY_LANE_FAIL_MARKER"
_P = _F = 0
def chk(c, m):
    global _P, _F
    if c: _P += 1; print(f"  ok   {m}")
    else: _F += 1; print(f"  FAIL {m}")
def main():
    # marker on (default): returns a machine-readable UNVERIFIED string carrying the reason
    os.environ.pop(FLAG, None)  # default on
    r = gw.lane_unverified_reply("engine shim timed out after 600s")
    chk(isinstance(r, str) and r.startswith("LANE_UNVERIFIED:"), "default on: starts with LANE_UNVERIFIED:")
    chk("timed out" in r, "carries the reason")
    chk("rejected" not in r.lower(), "must NOT say 'rejected' (the whole point)")
    # marker off: empty -> old bare behavior (caller falls back to raising)
    os.environ[FLAG] = "0"
    chk(gw.lane_unverified_reply("x") == "", "off: empty string (restore bare-error behavior)")
    os.environ.pop(FLAG, None)
    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0
if __name__ == "__main__":
    sys.exit(main())
