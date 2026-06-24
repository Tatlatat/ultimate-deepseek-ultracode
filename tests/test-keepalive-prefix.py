from __future__ import annotations
import importlib.util, os, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def test_records_prefix_head_for_keepalive():
    # When a lane runs, the gateway remembers the SHARED PREFIX HEAD (the leading bytes
    # that DeepSeek caches) keyed by prefix-family, so a background keep-alive can later
    # re-touch it to refresh LRU recency and stop an idle eviction between workflows.
    gw._KEEPALIVE_PREFIXES.clear()
    prompt = "SHARED CODEBASE BLOCK " + ("x" * 9000) + "\nangle: race conditions."
    gw.record_keepalive_prefix(prompt)
    key = gw.prefix_prime_key(prompt)
    expect(key in gw._KEEPALIVE_PREFIXES, "prefix head recorded under its family key")
    head, ts = gw._KEEPALIVE_PREFIXES[key]
    # The stored head must be a byte-identical leading slice of the prompt (so a ping
    # carrying it hits the same cached prefix), not the volatile tail.
    expect(prompt.startswith(head), "stored head is the literal leading slice")
    expect(len(head) >= 1000, "head is large enough to be a meaningful cache prefix")


def test_keepalive_targets_are_recent_only():
    # The keep-alive should ping only families seen within the freshness window, so we
    # don't waste pings re-warming prefixes the user has moved on from.
    os.environ["CLAUDE_REASONIX_GATEWAY_KEEPALIVE_WINDOW_SECONDS"] = "60"
    gw._KEEPALIVE_PREFIXES.clear()
    fresh = "FRESH PREFIX " + ("a" * 9000) + "\nq1"
    stale = "STALE PREFIX " + ("b" * 9000) + "\nq2"
    gw.record_keepalive_prefix(stale)
    # backdate the stale entry beyond the window
    skey = gw.prefix_prime_key(stale)
    head, _ = gw._KEEPALIVE_PREFIXES[skey]
    gw._KEEPALIVE_PREFIXES[skey] = (head, time.time() - 120)
    gw.record_keepalive_prefix(fresh)
    targets = gw.keepalive_targets()
    fkey = gw.prefix_prime_key(fresh)
    keys = {t[0] for t in targets}
    expect(fkey in keys, "fresh family is a keep-alive target")
    expect(skey not in keys, "stale family (past the window) is NOT a target")
    os.environ.pop("CLAUDE_REASONIX_GATEWAY_KEEPALIVE_WINDOW_SECONDS", None)


def test_keepalive_disabled_records_nothing():
    os.environ["CLAUDE_REASONIX_GATEWAY_KEEPALIVE"] = "0"
    gw._KEEPALIVE_PREFIXES.clear()
    try:
        gw.record_keepalive_prefix("ANYTHING " + ("z" * 9000))
        expect(len(gw._KEEPALIVE_PREFIXES) == 0, "kill-switch: no prefixes recorded")
    finally:
        os.environ.pop("CLAUDE_REASONIX_GATEWAY_KEEPALIVE", None)


def test_keepalive_dict_is_bounded():
    os.environ.pop("CLAUDE_REASONIX_GATEWAY_KEEPALIVE", None)
    os.environ["CLAUDE_REASONIX_GATEWAY_PRIME_DICT_CAP"] = "32"
    gw._KEEPALIVE_PREFIXES.clear()
    try:
        for i in range(200):
            gw.record_keepalive_prefix(f"DISTINCT {i} " + ("q" * 9000))
        expect(len(gw._KEEPALIVE_PREFIXES) <= 32, f"keepalive dict bounded; got {len(gw._KEEPALIVE_PREFIXES)}")
    finally:
        os.environ.pop("CLAUDE_REASONIX_GATEWAY_PRIME_DICT_CAP", None)
        gw._KEEPALIVE_PREFIXES.clear()


if __name__ == "__main__":
    test_records_prefix_head_for_keepalive()
    test_keepalive_targets_are_recent_only()
    test_keepalive_disabled_records_nothing()
    test_keepalive_dict_is_bounded()
    print("PASS: keepalive prefix")
