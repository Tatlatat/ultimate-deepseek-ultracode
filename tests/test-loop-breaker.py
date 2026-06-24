from __future__ import annotations
import importlib.util, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def test_counts_increment_per_signature():
    os.environ["CLAUDE_REASONIX_GATEWAY_MAX_LANE_RETRIES"] = "3"
    p = "SAME LANE SIGNATURE " * 600
    c1 = gw.register_lane_attempt(p)
    c2 = gw.register_lane_attempt(p)
    expect(c2 == c1 + 1, f"count increments: {c1} -> {c2}")
    other = gw.register_lane_attempt("DIFFERENT LANE " * 600)
    expect(other == 1, "a different signature starts at 1")


def test_force_fallback_after_threshold():
    os.environ["CLAUDE_REASONIX_GATEWAY_MAX_LANE_RETRIES"] = "3"
    p = "LOOPY LANE " * 600
    seen_force = False
    for _ in range(5):
        gw.register_lane_attempt(p)
        if gw.should_force_fallback(p):
            seen_force = True
    expect(seen_force, "force-fallback triggers once count reaches threshold")


def test_disabled_when_zero():
    os.environ["CLAUDE_REASONIX_GATEWAY_MAX_LANE_RETRIES"] = "0"
    p = "NEVER FORCE " * 600
    for _ in range(10):
        gw.register_lane_attempt(p)
    expect(gw.should_force_fallback(p) is False, "threshold 0 disables loop-breaker")
    os.environ["CLAUDE_REASONIX_GATEWAY_MAX_LANE_RETRIES"] = "3"


if __name__ == "__main__":
    test_counts_increment_per_signature()
    test_force_fallback_after_threshold()
    test_disabled_when_zero()
    print("PASS: loop breaker")
