#!/usr/bin/env python3
"""Reject-on-overscope: flag off = always None (byte-inert); flag on = reject a lane
naming >N existing files or a bulk-codebase scope, else None."""
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
    os.environ.pop(FLAG, None)
    # OFF: always None (no rejection, byte-inert)
    chk(gw.overscope_rejection("audit the entire codebase", CWD) is None, "OFF: bulk scope -> None")
    chk(gw.overscope_rejection("read README.md", CWD) is None, "OFF: small lane -> None")
    os.environ[FLAG] = "1"
    # ON: small/normal lane -> None (not rejected)
    chk(gw.overscope_rejection("read the file README.md and summarize it", CWD) is None,
        "ON: 1-file lane -> None (allowed)")
    # ON: bulk non-enumerable scope -> rejection string
    r = gw.overscope_rejection("audit the entire codebase for bugs", CWD)
    chk(isinstance(r, str) and "decompose" in r.lower(), "ON: 'audit the entire codebase' -> reject string")
    chk(isinstance(gw.overscope_rejection("review all files under src", CWD), str), "ON: 'all files under src' -> reject")
    # ON: the BARE bulk phrasings (widened regex — the most common 833-file shapes)
    for bulk in ("audit the codebase", "analyze all the source files",
                 "look at everything in src/", "go through the whole repo",
                 "analyze the project for issues"):
        chk(isinstance(gw.overscope_rejection(bulk, CWD), str), f"ON: bulk '{bulk}' -> reject")
    # ON: legit narrow lanes must NOT be rejected (false-positive guard) — incl. the
    # "<verb> the project <noun>" borderline that a bare project-scope regex would steal.
    for narrow in ("read src/auth.py and check it", "analyze the auth module in src/auth.py",
                   "fix the bug in handler.py", "explain the prime gate logic",
                   "read the project README", "read the project README.md and summarize",
                   "review the project plan in docs/x.md", "check the project layout"):
        chk(gw.overscope_rejection(narrow, CWD) is None, f"ON: narrow '{narrow}' -> None (no false reject)")
    # ON: >N explicit existing files -> rejection (build a prompt naming 11 real files)
    import glob
    many = [os.path.relpath(p, CWD) for p in glob.glob(str(ROOT / "tests" / "test-*.py"))][:11]
    if len(many) >= 11:
        prompt = "read these files: " + " ".join(many)
        chk(isinstance(gw.overscope_rejection(prompt, CWD), str), "ON: 11 named files -> reject")
    os.environ.pop(FLAG, None)
    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0
if __name__ == "__main__":
    sys.exit(main())
