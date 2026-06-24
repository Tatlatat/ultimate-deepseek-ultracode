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


# A nested array-of-objects StructuredOutput schema — both reader and synthesize
# lanes in the real deep-research / research-system workflows carry one of these.
NESTED_TOOLS = [{
    "name": "StructuredOutput",
    "input_schema": {
        "type": "object",
        "properties": {
            "keyComponents": {"type": "array", "items": {
                "type": "object", "properties": {"name": {"type": "string"}}}},
        },
        "required": ["keyComponents"],
    },
}]

# Real reader-lane prompt shape (read files and report) — must NOT get map-reduce.
READER_PROMPT = (
    "Read the TikTok video generation engine in /Users/x/apps/tiktok-video-automation. "
    "Read the README, cli.py, footage_engine.py, beat_grid.py, music_analysis.py, subtitles.py. "
    "Also list what's in tests/ to gauge maturity. Explain the full pipeline. "
    + ("padding to exceed the 20k length floor. " * 600)
)

# Real synthesize-lane prompt shape (merge many items) — SHOULD get map-reduce.
SYNTH_PROMPT = (
    "Synthesize the following research items into one final JSON object matching the schema. "
    "Merge semantic duplicates across all findings and rank by confidence. "
    + ("ITEM padding to exceed the 20k length floor. " * 600)
)


def test_reader_lane_does_not_get_mapreduce():
    os.environ.pop("CLAUDE_REASONIX_GATEWAY_MAPREDUCE_SYNTHESIS", None)
    expect(gw.is_synthesis_prompt(READER_PROMPT) is False,
           "a read-files-and-report prompt is NOT synthesis intent")
    expect(gw.is_heavy_synthesis(NESTED_TOOLS, len(READER_PROMPT), READER_PROMPT) is False,
           "reader lane (nested schema + long) must NOT trigger map-reduce")


def test_synthesize_lane_gets_mapreduce():
    os.environ.pop("CLAUDE_REASONIX_GATEWAY_MAPREDUCE_SYNTHESIS", None)
    expect(gw.is_synthesis_prompt(SYNTH_PROMPT) is True,
           "a merge-many-items prompt IS synthesis intent")
    expect(gw.is_heavy_synthesis(NESTED_TOOLS, len(SYNTH_PROMPT), SYNTH_PROMPT) is True,
           "synthesize lane (nested schema + long + intent) SHOULD trigger map-reduce")


def test_short_synthesis_still_skipped():
    # Even a synthesize prompt below the length floor is left alone (small merges
    # don't loop). Guards against firing the skill on trivial merges.
    short = "Synthesize and merge the following items into one JSON object."
    expect(gw.is_heavy_synthesis(NESTED_TOOLS, len(short), short) is False,
           "below the length floor, even synthesis intent is skipped")


def test_kill_switch_disables():
    os.environ["CLAUDE_REASONIX_GATEWAY_MAPREDUCE_SYNTHESIS"] = "0"
    try:
        expect(gw.is_heavy_synthesis(NESTED_TOOLS, len(SYNTH_PROMPT), SYNTH_PROMPT) is False,
               "kill-switch disables map-reduce entirely")
    finally:
        os.environ.pop("CLAUDE_REASONIX_GATEWAY_MAPREDUCE_SYNTHESIS", None)


if __name__ == "__main__":
    test_reader_lane_does_not_get_mapreduce()
    test_synthesize_lane_gets_mapreduce()
    test_short_synthesis_still_skipped()
    test_kill_switch_disables()
    print("PASS: map-reduce synthesis-only gating")
