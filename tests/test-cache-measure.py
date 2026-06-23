from __future__ import annotations
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "codex-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def test_weighted_cache_basic():
    rows = [
        {"input_tokens": 1000, "cache_pct": 100.0},
        {"input_tokens": 1000, "cache_pct": 0.0},
    ]
    r = gw.weighted_cache(rows)
    expect(abs(r["weighted_pct"] - 50.0) < 1e-6, f"50% expected, got {r['weighted_pct']}")
    expect(r["total_in"] == 2000, "total_in")
    expect(r["total_miss"] == 1000, "total_miss")
    expect(r["n"] == 2, "n counts rows with cache data")


def test_weighted_cache_ignores_missing_cache():
    rows = [{"input_tokens": 500, "cache_pct": None}, {"input_tokens": 500, "cache_pct": 90.0}]
    r = gw.weighted_cache(rows)
    expect(r["n"] == 1, "only the row with numeric cache_pct counts")
    expect(abs(r["weighted_pct"] - 90.0) < 1e-6, "90%")


def test_weighted_cache_empty():
    r = gw.weighted_cache([])
    expect(r["weighted_pct"] == 0.0 and r["total_in"] == 0 and r["n"] == 0, "empty safe")


def test_classify_miss_buckets():
    rows = [
        {"input_tokens": 200_000, "cache_pct": 82.0},  # loop_inflation (>150k)
        {"input_tokens": 10_000, "cache_pct": 40.0},    # unique_tail (<60% & small)
        {"input_tokens": 10_000, "cache_pct": 85.0},    # cold_prefix (mid)
    ]
    c = gw.classify_miss(rows)
    expect(c["loop_inflation"] > 0, "big lane miss is loop_inflation")
    expect(c["unique_tail"] > 0, "low-cache small lane miss is unique_tail")
    expect(c["cold_prefix"] > 0, "mid-cache lane miss is cold_prefix")


if __name__ == "__main__":
    test_weighted_cache_basic()
    test_weighted_cache_ignores_missing_cache()
    test_weighted_cache_empty()
    test_classify_miss_buckets()
    print("PASS: cache measure")
