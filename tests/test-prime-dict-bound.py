from __future__ import annotations
import importlib.util, os, threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "codex-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def _clear_all():
    for d in (gw._PRIME_GATES, gw._PRIME_SERIAL_COUNTS, gw._PRIME_SERIAL_LOCKS, gw._LANE_COUNTS):
        d.clear()


def test_prime_gates_bounded():
    # The prime-gate dicts grew unbounded (a leak across a long session — every
    # distinct prefix-family hash added an entry that was never removed). With the
    # bound, after many DISTINCT keys the dict size stays <= the cap.
    os.environ["CLAUDE_CODEX_GATEWAY_PRIME_GATE"] = "1"
    os.environ["CLAUDE_CODEX_GATEWAY_PRIME_DICT_CAP"] = "64"
    _clear_all()
    try:
        for i in range(500):
            # each distinct prompt -> distinct prime_key -> a new gate entry
            gw.acquire_prime_role("DISTINCT PROMPT NUMBER " + str(i) + " " + ("x" * 5000))
        expect(len(gw._PRIME_GATES) <= 64,
               f"_PRIME_GATES bounded to cap; got {len(gw._PRIME_GATES)}")
    finally:
        os.environ.pop("CLAUDE_CODEX_GATEWAY_PRIME_DICT_CAP", None)
        _clear_all()


def test_lane_counts_bounded():
    os.environ["CLAUDE_CODEX_GATEWAY_PRIME_DICT_CAP"] = "64"
    _clear_all()
    try:
        for i in range(500):
            gw.register_lane_attempt("UNIQUE LANE " + str(i) + " " + ("y" * 5000))
        expect(len(gw._LANE_COUNTS) <= 64,
               f"_LANE_COUNTS bounded; got {len(gw._LANE_COUNTS)}")
    finally:
        os.environ.pop("CLAUDE_CODEX_GATEWAY_PRIME_DICT_CAP", None)
        _clear_all()


def test_live_burst_key_not_evicted():
    # The eviction must keep the MOST RECENT keys: a key just inserted (a live burst)
    # must still be findable so its waiters get the SAME gate as its primer. Insert a
    # key, then flood with other keys up to the cap; the live key's gate must persist.
    os.environ["CLAUDE_CODEX_GATEWAY_PRIME_GATE"] = "1"
    os.environ["CLAUDE_CODEX_GATEWAY_PRIME_DICT_CAP"] = "32"
    _clear_all()
    try:
        live_prompt = "LIVE BURST PROMPT " + ("z" * 5000)
        is_primer, gate = gw.acquire_prime_role(live_prompt)
        expect(is_primer is True, "first caller is primer")
        # flood with cap-1 OTHER distinct keys (stays within cap, live key is newest-ish)
        for i in range(20):
            gw.acquire_prime_role("OTHER " + str(i) + " " + ("q" * 5000))
        # a waiter for the live prompt must get the SAME gate (not a fresh primer)
        is_primer2, gate2 = gw.acquire_prime_role(live_prompt)
        expect(is_primer2 is False and gate2 is gate,
               "live burst key still maps to the same gate after other keys arrive")
    finally:
        os.environ.pop("CLAUDE_CODEX_GATEWAY_PRIME_DICT_CAP", None)
        _clear_all()


def test_cap_disabled_keeps_all():
    os.environ["CLAUDE_CODEX_GATEWAY_PRIME_DICT_CAP"] = "0"
    _clear_all()
    try:
        for i in range(100):
            gw.acquire_prime_role("KEYNUM " + str(i) + " " + ("w" * 5000))
        expect(len(gw._PRIME_GATES) == 100, "cap=0 disables eviction (keep all)")
    finally:
        os.environ.pop("CLAUDE_CODEX_GATEWAY_PRIME_DICT_CAP", None)
        _clear_all()


if __name__ == "__main__":
    test_prime_gates_bounded()
    test_lane_counts_bounded()
    test_live_burst_key_not_evicted()
    test_cap_disabled_keeps_all()
    print("PASS: prime dict bound")
