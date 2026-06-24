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


def test_directive_is_lean_without_hard_cap():
    os.environ.pop("CLAUDE_REASONIX_GATEWAY_CONTEXT_GUARD", None)
    d = gw.context_budget_directive()
    # The guard must steer the lane to work targeted (grep/glob, no whole-dir dumps)
    # WITHOUT a hard file cap that would kill a task genuinely needing many files.
    expect("file" in d.lower(), "directive mentions files")
    expect("grep" in d.lower() or "glob" in d.lower() or "targeted" in d.lower(),
           "directive steers toward targeted search, not whole-dir reads")


def test_directive_tells_lane_to_flag_oversized_work():
    # The key behaviour replacing the hard cap: an oversized lane must FLAG that the
    # task should be split, not silently cram everything into one lane.
    d = gw.context_budget_directive()
    expect("split" in d.lower() or "smaller lanes" in d.lower() or "too large" in d.lower(),
           "directive tells an oversized lane to flag for decomposition")
    expect("ONE lane" in d or "one lane" in d.lower(),
           "directive references doing the work in a single lane vs splitting")


def test_disabled_returns_empty():
    os.environ["CLAUDE_REASONIX_GATEWAY_CONTEXT_GUARD"] = "0"
    try:
        expect(gw.context_budget_directive() == "", "kill-switch disables the guard")
    finally:
        os.environ.pop("CLAUDE_REASONIX_GATEWAY_CONTEXT_GUARD", None)


def test_guard_is_prefix_stable():
    # The guard text must be identical across calls (lane-invariant) so it can sit at
    # the FRONT of the prompt without breaking the shared DeepSeek prefix.
    os.environ.pop("CLAUDE_REASONIX_GATEWAY_CONTEXT_GUARD", None)
    os.environ.pop("CLAUDE_REASONIX_GATEWAY_MAX_FILE_READS", None)
    a = gw.context_budget_directive()
    b = gw.context_budget_directive()
    expect(a == b, "guard directive is byte-identical across calls")
    expect(a != "", "guard is on by default")


def test_guard_present_in_assembled_prompt_for_tool_lane():
    # A lane that has tools (can read files) should carry the guard near the front.
    msgs = [{"role": "user", "content": "Read the whole repo and report."}]
    tools = [{"name": "read_file", "input_schema": {"type": "object"}}]
    out = gw.openai_messages_to_prompt(msgs, tools)
    expect("WORK LEAN" in out or "work lean" in out.lower(),
           "assembled tool-lane prompt carries the context-budget guard")


def test_no_guard_for_toolless_lane():
    # A lane with no tools cannot read files, so it should NOT carry the guard.
    msgs = [{"role": "user", "content": "Summarize this text."}]
    out = gw.openai_messages_to_prompt(msgs, None)
    expect("WORK LEAN" not in out, "tool-less lane gets no read guard")


if __name__ == "__main__":
    test_directive_is_lean_without_hard_cap()
    test_directive_tells_lane_to_flag_oversized_work()
    test_disabled_returns_empty()
    test_guard_is_prefix_stable()
    test_guard_present_in_assembled_prompt_for_tool_lane()
    test_no_guard_for_toolless_lane()
    print("PASS: context budget guard")
