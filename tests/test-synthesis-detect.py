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


NESTED = [{"name": "StructuredOutput", "input_schema": {
    "type": "object", "required": ["findings"], "properties": {
        "findings": {"type": "array", "items": {"type": "object",
            "properties": {"claim": {"type": "string"}, "sources": {"type": "array"}}}}}}}]
FLAT = [{"name": "StructuredOutput", "input_schema": {
    "type": "object", "properties": {"refuted": {"type": "boolean"}}}}]


def test_heavy_when_nested_and_large():
    expect(gw.is_heavy_synthesis(NESTED, 40000) is True, "nested + large -> heavy")


def test_not_heavy_when_small():
    expect(gw.is_heavy_synthesis(NESTED, 5000) is False, "nested but small -> not heavy")


def test_not_heavy_when_flat():
    expect(gw.is_heavy_synthesis(FLAT, 40000) is False, "flat schema -> not heavy even if large")


def test_directive_mentions_skill():
    d = gw.mapreduce_directive()
    expect("map-reduce-synthesis" in d, "directive names the skill")
    expect("run_skill" in d, "directive tells reasonix to run_skill")


def test_killswitch_off():
    os.environ["CLAUDE_CODEX_GATEWAY_MAPREDUCE_SYNTHESIS"] = "0"
    try:
        expect(gw.is_heavy_synthesis(NESTED, 40000) is False, "killswitch off -> never heavy")
    finally:
        os.environ.pop("CLAUDE_CODEX_GATEWAY_MAPREDUCE_SYNTHESIS", None)


if __name__ == "__main__":
    test_heavy_when_nested_and_large()
    test_not_heavy_when_small()
    test_not_heavy_when_flat()
    test_directive_mentions_skill()
    test_killswitch_off()
    print("PASS: synthesis detect")
