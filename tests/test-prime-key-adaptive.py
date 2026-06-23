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


def test_lanes_sharing_only_a_short_head_get_same_key():
    # Two lanes share ~5KB of a longer prompt then diverge (the real fan-out shape:
    # shared system+intro, then a per-lane DIMENSION line, then file content).
    # The OLD 32KB key hashed the whole prompt and split them; the adaptive key
    # must group them so the prime gate can prime the shared head once.
    shared = "SYSTEM shared header. " * 250  # ~5.5KB common
    a = shared + "DIMENSION: CORRECTNESS\n" + "file body A " * 2000
    b = shared + "DIMENSION: CACHE\n" + "file body B " * 2000
    os.environ.pop("CLAUDE_CODEX_GATEWAY_PRIME_KEY_HEAD", None)
    expect(gw.prefix_prime_key(a) == gw.prefix_prime_key(b),
           "lanes sharing the leading head must share a prime key (adaptive)")


def test_truly_different_lanes_get_different_keys():
    a = "COMPLETELY DIFFERENT START A " * 300
    b = "ANOTHER UNRELATED PROMPT B " * 300
    expect(gw.prefix_prime_key(a) != gw.prefix_prime_key(b),
           "lanes with different leading content get different keys")


def test_key_head_env_override():
    # The adaptive head length is configurable; a tiny head groups almost anything.
    os.environ["CLAUDE_CODEX_GATEWAY_PRIME_KEY_HEAD"] = "2048"
    try:
        base = "X" * 2048
        expect(gw.prefix_prime_key(base + "aaaa") == gw.prefix_prime_key(base + "bbbb"),
               "with 2KB head, prompts sharing first 2KB share a key")
    finally:
        os.environ.pop("CLAUDE_CODEX_GATEWAY_PRIME_KEY_HEAD", None)


if __name__ == "__main__":
    test_lanes_sharing_only_a_short_head_get_same_key()
    test_truly_different_lanes_get_different_keys()
    test_key_head_env_override()
    print("PASS: prime key adaptive")
