#!/usr/bin/env python3
"""A2: the over-broad audit-shaped lane (level-3.1 failure shape) must be rejected when
OVERSCOPE_REJECT is on, and None when off (byte-inert)."""
import importlib.util, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(gw)
FLAG = "CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT"
CWD = str(ROOT)
_P = _F = 0
def chk(c, m):
    global _P, _F
    if c: _P += 1; print(f"  ok   {m}")
    else: _F += 1; print(f"  FAIL {m}")
def main():
    # the real level-3.1 audit lane that timed out — names a directory-wide scope
    audit_lane = "Review SECURITY của API mobile: audit the whole codebase under lib/mobile and app/api"
    os.environ.pop(FLAG, None)
    chk(gw.overscope_rejection(audit_lane, CWD) is None, "OFF: audit lane -> None (byte-inert)")
    os.environ[FLAG] = "1"
    r = gw.overscope_rejection(audit_lane, CWD)
    chk(isinstance(r, str) and "decompose" in r.lower(), "ON: bulk audit lane -> reject string")
    # a narrow real lane must still pass
    chk(gw.overscope_rejection("read lib/mobile/jwt.ts and check exp validation", CWD) is None,
        "ON: narrow 1-file lane -> None (no false reject)")
    os.environ.pop(FLAG, None)
    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0
if __name__ == "__main__":
    sys.exit(main())
