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


NESTED = [{"name": "StructuredOutput", "input_schema": {
    "type": "object", "required": ["findings"], "properties": {
        "findings": {"type": "array", "items": {"type": "object",
            "properties": {"claim": {"type": "string"}, "sources": {"type": "array"}}}}}}}]
FLAT = [{"name": "StructuredOutput", "input_schema": {
    "type": "object", "properties": {"refuted": {"type": "boolean"}}}}]

# map-reduce is a Synthesize-phase tool only, so is_heavy_synthesis now also requires
# the prompt to express synthesize/merge intent (a reader lane must never get it).
SYNTH = "Synthesize and merge the following findings into one JSON object. " + ("x " * 100)


def test_heavy_when_nested_and_large_and_synthesis():
    expect(gw.is_heavy_synthesis(NESTED, 40000, SYNTH) is True,
           "nested + large + synthesis intent -> heavy")


def test_not_heavy_when_small():
    expect(gw.is_heavy_synthesis(NESTED, 5000, SYNTH) is False, "nested but small -> not heavy")


def test_not_heavy_when_flat():
    expect(gw.is_heavy_synthesis(FLAT, 40000, SYNTH) is False, "flat schema -> not heavy even if large")


def test_not_heavy_when_reader_intent():
    reader = "Read the README and cli.py and report the pipeline. " + ("x " * 100)
    expect(gw.is_heavy_synthesis(NESTED, 40000, reader) is False,
           "reader lane -> not heavy even with nested schema + large prompt")


def test_directive_mentions_skill():
    d = gw.mapreduce_directive()
    expect("map-reduce-synthesis" in d, "directive names the skill")
    expect("run_skill" in d, "directive tells reasonix to run_skill")


def test_killswitch_off():
    os.environ["CLAUDE_REASONIX_GATEWAY_MAPREDUCE_SYNTHESIS"] = "0"
    try:
        expect(gw.is_heavy_synthesis(NESTED, 40000, SYNTH) is False, "killswitch off -> never heavy")
    finally:
        os.environ.pop("CLAUDE_REASONIX_GATEWAY_MAPREDUCE_SYNTHESIS", None)


if __name__ == "__main__":
    test_heavy_when_nested_and_large_and_synthesis()
    test_not_heavy_when_small()
    test_not_heavy_when_flat()
    test_not_heavy_when_reader_intent()
    test_directive_mentions_skill()
    test_killswitch_off()
    print("PASS: synthesis detect")
