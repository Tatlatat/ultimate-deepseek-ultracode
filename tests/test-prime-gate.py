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


def test_key_groups_lanes_sharing_leading_head():
    # The prime key hashes only the leading head (~4KB) so lanes that share that
    # head but diverge later (the real fan-out shape) get the SAME key and the
    # gate can prime once for all of them.
    import os
    os.environ.pop("CLAUDE_CODEX_GATEWAY_PRIME_KEY_HEAD", None)
    os.environ.pop("CLAUDE_CODEX_GATEWAY_PRIME_HEAD_BYTES", None)
    head = "SHARED HEAD " * 500  # ~6KB common, beyond the 4KB key window
    a = head + "DIMENSION A " + "tail A " * 3000
    b = head + "DIMENSION B " + "tail B " * 3000
    expect(gw.prefix_prime_key(a) == gw.prefix_prime_key(b),
           "lanes sharing the leading head share a prime key")
    # Lanes that differ WITHIN the leading head get different keys.
    c = "DIFFERENT START " + head
    expect(gw.prefix_prime_key(a) != gw.prefix_prime_key(c),
           "differ inside the head -> different key")


def test_grace_env_default():
    # The post-open grace setting must exist and default to 1.5.
    os.environ.pop("CLAUDE_CODEX_GATEWAY_PRIME_GRACE_SECONDS", None)
    expect(abs(gw.env_float("CLAUDE_CODEX_GATEWAY_PRIME_GRACE_SECONDS", default=1.5) - 1.5) < 1e-9,
           "grace default is 1.5")


def test_primer_then_waiter_roles():
    os.environ["CLAUDE_CODEX_GATEWAY_PRIME_GATE"] = "1"
    p = "PRIME ROLE " * 4000
    isp1, g1 = gw.acquire_prime_role(p)
    isp2, g2 = gw.acquire_prime_role(p)
    expect(isp1 is True and isp2 is False, "first=primer, second=waiter")
    expect(g1 is g2, "same prefix -> same gate")


def test_disabled_passthrough():
    os.environ["CLAUDE_CODEX_GATEWAY_PRIME_GATE"] = "0"
    isp, g = gw.acquire_prime_role("anything")
    expect(isp is False and g is None, "disabled -> passthrough")
    os.environ["CLAUDE_CODEX_GATEWAY_PRIME_GATE"] = "1"


if __name__ == "__main__":
    test_key_groups_lanes_sharing_leading_head()
    test_grace_env_default()
    test_primer_then_waiter_roles()
    test_disabled_passthrough()
    print("PASS: prime gate")
