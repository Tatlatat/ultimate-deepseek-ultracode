#!/usr/bin/env python3
"""Two bugs the HONEST deno_lint harness run surfaced (both made the harness escalate
falsely on correct work):

BUG 1 — overscope false-positive on a NEGATED scope phrase: the orchestrator wrote
  "read ONLY these files, do NOT read the whole repo" (a SCOPE-NARROWING instruction),
  but _OVERSCOPE_BULK_RE matched "the whole repo" and rejected the lane. The guard must
  not fire when the bulk phrase is negated ("do not / don't / never ... the whole repo").

BUG 2 — lane_acceptance_test returned the WHOLE line after "ACCEPTANCE_TEST:", including
  prose the model appended ("cargo test --lib x passes WITH the added cases"), so the shim
  execSync ran `cargo test --lib x passes ...` -> "unexpected argument 'passes'" -> the
  acceptance test failed for a SPURIOUS reason -> the lane stagnated though the code was
  correct. lane_acceptance_test must extract just the SHELL COMMAND.
"""
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

def main():
    os.environ["CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT"] = "1"

    # ---- BUG 1: negated bulk phrase must NOT be rejected ----
    neg = ("Extract the parser into a new module. To stay cheap, read ONLY these files, "
           "do not read the whole repo: src/a.ts, src/b.ts. ACCEPTANCE_TEST: bun test x")
    chk(gw.overscope_rejection(neg, "/tmp") is None,
        "BUG1: 'do not read the whole repo' (negated) -> NOT rejected")

    neg2 = "Don't scan the entire codebase; only touch src/foo.rs. Add a helper there."
    chk(gw.overscope_rejection(neg2, "/tmp") is None,
        "BUG1: \"don't scan the entire codebase\" (negated) -> NOT rejected")

    # a GENUINE bulk lane (not negated) must STILL be rejected
    real = "Audit the whole repo and refactor every module you find."
    chk(gw.overscope_rejection(real, "/tmp") is not None,
        "BUG1: genuine 'audit the whole repo ... every module' -> STILL rejected")
    real2 = "Go through the entire codebase and fix all the call sites."
    chk(gw.overscope_rejection(real2, "/tmp") is not None,
        "BUG1: genuine 'go through the entire codebase' -> STILL rejected")

    # ---- BUG 2: acceptance line must extract ONLY the command ----
    def at(line):
        return gw.lane_acceptance_test([{"role": "user", "content": f"do x\nACCEPTANCE_TEST: {line}\nmore"}])

    chk(at("cargo test --lib no_useless_catch passes WITH the added realistic cases")
        == "cargo test --lib no_useless_catch",
        "BUG2: 'cargo test --lib X passes WITH...' -> 'cargo test --lib X'")

    chk(at("cargo test --lib no_useless_catch  (must pass) AND cargo check --lib (must stay green)")
        in ("cargo test --lib no_useless_catch && cargo check --lib",
            "cargo test --lib no_useless_catch"),
        "BUG2: 'X (must pass) AND Y (must stay green)' -> clean command(s), no prose")

    chk(at("cargo test --lib no_useless_catch passes AND cargo check --lib passes")
        in ("cargo test --lib no_useless_catch && cargo check --lib",
            "cargo test --lib no_useless_catch"),
        "BUG2: 'X passes AND Y passes' -> clean command(s)")

    # a clean command (no prose) must pass through UNCHANGED (byte-inert for good input)
    chk(at("bun test src/x.test.ts") == "bun test src/x.test.ts",
        "BUG2: a clean command is unchanged")
    chk(at("cargo test --lib foo") == "cargo test --lib foo",
        "BUG2: clean cargo command unchanged")

    # quoted/backtick command extracts the inside
    chk(at("`cargo test --lib foo`") == "cargo test --lib foo",
        "BUG2: backtick-wrapped command -> unwrapped")

    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0

if __name__ == "__main__":
    sys.exit(main())
