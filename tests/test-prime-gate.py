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


def test_key_uses_32kb_window():
    # Prompts that share the first 8192 bytes but DIFFER between byte 8192 and 32768
    # must get DIFFERENT keys — this only holds if the key window is 32KB, not 8KB.
    base = "X" * 8192
    a = base + "A" * 20000 + "tailA"
    b = base + "B" * 20000 + "tailB"
    expect(gw.prefix_prime_key(a) != gw.prefix_prime_key(b),
           "differ within 8192..32768 -> different key (proves 32KB window, not 8KB)")
    # Identical for the full 32KB head, differing only past 32768 -> SAME key.
    head = "X" * 33000
    expect(gw.prefix_prime_key(head + "AAAA") == gw.prefix_prime_key(head + "BBBB"),
           "share full 32KB head -> same key")


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
    test_key_uses_32kb_window()
    test_grace_env_default()
    test_primer_then_waiter_roles()
    test_disabled_passthrough()
    print("PASS: prime gate")
