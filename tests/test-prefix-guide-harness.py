#!/usr/bin/env python3
"""C1: PREFIX_GUIDE point 11 must instruct instance-level specs + complete-subtask lanes
+ the ACCEPTANCE_TEST line the harness consumes, and must NOT have been removed when off."""
import importlib.util, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("h", ROOT / "hooks" / "reasonix-workflow.py")
h = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(h)
_P = _F = 0
def chk(c, m):
    global _P, _F
    if c: _P += 1; print(f"  ok   {m}")
    else: _F += 1; print(f"  FAIL {m}")
def main():
    g = h.PREFIX_GUIDE_TEXT
    chk("11." in g, "point 11 present")
    chk("ACCEPTANCE_TEST:" in g, "tells orchestrator to emit ACCEPTANCE_TEST line (harness reads it)")
    chk("instance" in g.lower() and ("complete" in g.lower() or "draft+" in g.lower() or "edit + verify" in g.lower()),
        "instructs instance-level specs + complete sub-task lanes")
    chk("do not" in g.lower() and ("repo" in g.lower() or "dump" in g.lower() or "few-shot" in g.lower()),
        "warns against repo-dump / few-shot (measured to hurt)")
    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0
if __name__ == "__main__":
    sys.exit(main())
