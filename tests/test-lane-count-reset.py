from __future__ import annotations
import importlib.util, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "codex-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def test_success_resets_lane_count():
    # The loop-breaker counts attempts per prefix-family. Without a reset on success
    # it self-poisons: once a family hits MAX_LANE_RETRIES cumulatively across the
    # session, a FRESH healthy lane of that family that narrates once is force-
    # fallback'd instead of retried. A successful lane (parseable output) means the
    # family is NOT stuck looping, so the count must reset.
    os.environ["CLAUDE_CODEX_GATEWAY_MAX_LANE_RETRIES"] = "3"
    gw._LANE_COUNTS.clear()
    p = "SAME FAMILY PROMPT " + ("x" * 5000)
    gw.register_lane_attempt(p)
    gw.register_lane_attempt(p)
    gw.register_lane_attempt(p)
    expect(gw.should_force_fallback(p) is True, "3 attempts -> would force fallback")
    # A lane of the same family now SUCCEEDS -> reset the family's count.
    gw.clear_lane_count(p)
    expect(gw.should_force_fallback(p) is False,
           "after a success, the family is no longer force-fallback'd")
    gw._LANE_COUNTS.clear()


def test_reset_is_per_family():
    os.environ["CLAUDE_CODEX_GATEWAY_MAX_LANE_RETRIES"] = "3"
    gw._LANE_COUNTS.clear()
    a = "FAMILY A " + ("a" * 5000)
    b = "FAMILY B " + ("b" * 5000)
    for _ in range(3):
        gw.register_lane_attempt(a)
        gw.register_lane_attempt(b)
    gw.clear_lane_count(a)  # only A succeeds
    expect(gw.should_force_fallback(a) is False, "A reset")
    expect(gw.should_force_fallback(b) is True, "B untouched (still poisoned until it succeeds)")
    gw._LANE_COUNTS.clear()


def test_clear_unknown_key_is_safe():
    gw._LANE_COUNTS.clear()
    gw.clear_lane_count("NEVER SEEN " + ("z" * 5000))  # must not raise
    print("ok")


if __name__ == "__main__":
    test_success_resets_lane_count()
    test_reset_is_per_family()
    test_clear_unknown_key_is_safe()
    print("PASS: lane count reset")
