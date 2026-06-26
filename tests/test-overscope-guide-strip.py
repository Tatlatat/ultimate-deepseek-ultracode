#!/usr/bin/env python3
"""BUGFIX: the gateway-injected PREFIX_GUIDE advisory ('...every file under review...')
must NOT trip the overscope guard. overscope_rejection classifies the LANE TASK, not the
injected cache-advice. A narrow lane stays narrow even with the guide prepended."""
import importlib.util, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)

_P = _F = 0
def chk(c, m):
    global _P, _F
    if c: _P += 1; print(f"  ok   {m}")
    else: _F += 1; print(f"  FAIL {m}")

# A representative slice of the real PREFIX_GUIDE: it contains "every file" (point 4) and
# "everything in" (point 6) — both bulk-regex tokens — inside ADVISORY prose, plus the
# stable opening marker and the advisory closer.
GUIDE = (
    "PROMPT-CACHE NOTE for this Dynamic Workflow: each agent() lane runs on\n"
    "DeepSeek via reasonix, where a cache MISS costs ~50x a hit.\n"
    "4. DURABLE shared block: build ONE fixed shared-context string ONCE (the full\n"
    "   text of every file under review + the common instructions).\n"
    "6. DECOMPOSE FINELY: a big vague lane crams everything into one context.\n"
    "This is advisory — correctness first; apply where it doesn't distort the work."
)
NARROW = "Extract parseTestBlocks into a new module test-block-parser.ts. ACCEPTANCE_TEST: bun test x"
BULK = "Audit the entire codebase and refactor every module you find."

def main():
    os.environ["CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT"] = "1"

    # 1. the guide ALONE must strip to (almost) nothing scope-relevant -> not a bulk task
    stripped = gw._strip_injected_guide(GUIDE)
    chk("every file" not in stripped.lower() and "everything" not in stripped.lower(),
        "guide block is stripped (no 'every file'/'everything' survives)")

    # 2. a NARROW lane with the guide prepended must NOT be rejected (the bug)
    rej = gw.overscope_rejection(GUIDE + "\n\n" + NARROW, "/tmp")
    chk(rej is None, "narrow lane + injected guide -> NOT rejected (the bug is fixed)")

    # 3. the SAME narrow lane without the guide also passes (unchanged behavior)
    chk(gw.overscope_rejection(NARROW, "/tmp") is None, "narrow lane alone -> NOT rejected")

    # 4. a genuinely bulk lane STILL gets rejected, even with the guide prepended
    #    (we strip only the advisory, not the real task)
    chk(gw.overscope_rejection(GUIDE + "\n\n" + BULK, "/tmp") is not None,
        "bulk lane + guide -> STILL rejected (real over-broad task caught)")

    # 5. a bulk lane alone still rejected (regression guard)
    chk(gw.overscope_rejection(BULK, "/tmp") is not None, "bulk lane alone -> rejected")

    # 6. the read-classifier must NOT be flipped to 'edit' by the guide's edit-verb
    #    prose ('build ONE fixed shared block') — else Lever A's read-cap is disabled.
    read_task = "Read src/foo.ts and summarize its exports."
    chk(gw.classify_lane_type(None, read_task) == "read", "read task alone -> read")
    chk(gw.classify_lane_type(None, GUIDE + "\n\n" + read_task) == "read",
        "read task + guide -> still read (classifier guide-immune)")

    # 7. a real edit lane still classifies edit with the guide prepended (regression)
    edit_task = "Refactor src/foo.ts: extract parseX into a new module and rewire."
    chk(gw.classify_lane_type(None, GUIDE + "\n\n" + edit_task) == "edit",
        "edit task + guide -> still edit")

    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0

if __name__ == "__main__":
    sys.exit(main())
