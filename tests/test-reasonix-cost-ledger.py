#!/usr/bin/env python3
"""Tests for the per-session reasonix cost ledger + summary.

The gateway appends one JSONL record per reasonix lane to
runtime/reasonix-cost.jsonl; `summarize_reasonix_cost(path)` aggregates it.
Writing is fail-open: a broken ledger path must never break a lane.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("rx_cost_gw", ROOT / "codex-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def test_append_and_summarize():
    d = tempfile.mkdtemp()
    ledger = os.path.join(d, "reasonix-cost.jsonl")
    # two lanes
    gw.append_reasonix_cost(ledger, {
        "reasonix_cost_usd": 0.001, "reasonix_cache_pct": 90.0,
        "input_tokens": 100, "output_tokens": 10,
    }, cwd="/proj/a", model="deepseek-v4-flash", claude_equiv=0.05)
    gw.append_reasonix_cost(ledger, {
        "reasonix_cost_usd": 0.003, "reasonix_cache_pct": 80.0,
        "input_tokens": 300, "output_tokens": 20,
    }, cwd="/proj/a", model="deepseek-v4-flash", claude_equiv=0.15)

    summary = gw.summarize_reasonix_cost(ledger)
    expect(summary["lanes"] == 2, f"lanes: {summary}")
    expect(abs(summary["total_usd"] - 0.004) < 1e-9, f"total: {summary}")
    expect(abs(summary["claude_equiv_usd"] - 0.20) < 1e-9, f"claude_equiv: {summary}")
    expect(summary["input_tokens"] == 400, f"in_tok: {summary}")
    expect(summary["output_tokens"] == 30, f"out_tok: {summary}")
    # avg cache weighted-or-mean both land at 85.0 for these inputs
    expect(abs(summary["avg_cache_pct"] - 85.0) < 0.01, f"cache: {summary}")


def test_summarize_missing_file_is_empty():
    summary = gw.summarize_reasonix_cost("/nonexistent/reasonix-cost.jsonl")
    expect(summary["lanes"] == 0, f"missing file should be empty: {summary}")
    expect(summary["total_usd"] == 0.0, f"missing file total should be 0: {summary}")


def test_append_is_fail_open():
    # an un-writable path must not raise
    try:
        gw.append_reasonix_cost("/nonexistent-dir/cannot/write.jsonl", {
            "reasonix_cost_usd": 0.001, "reasonix_cache_pct": 50.0,
            "input_tokens": 1, "output_tokens": 1,
        }, cwd="/x", model="m", claude_equiv=0.01)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"FAIL: append_reasonix_cost must be fail-open, raised: {exc}")


def test_none_cost_skipped_gracefully():
    d = tempfile.mkdtemp()
    ledger = os.path.join(d, "reasonix-cost.jsonl")
    # a lane whose cost couldn't be captured (None) still records a row but
    # contributes 0 to the total — never crashes the summary.
    gw.append_reasonix_cost(ledger, {
        "reasonix_cost_usd": None, "reasonix_cache_pct": None,
        "input_tokens": 5, "output_tokens": 5,
    }, cwd="/x", model="m", claude_equiv=None)
    summary = gw.summarize_reasonix_cost(ledger)
    expect(summary["lanes"] == 1, f"lane counted: {summary}")
    expect(summary["total_usd"] == 0.0, f"None cost contributes 0: {summary}")


def main() -> int:
    test_append_and_summarize()
    test_summarize_missing_file_is_empty()
    test_append_is_fail_open()
    test_none_cost_skipped_gracefully()
    print("PASS: reasonix cost ledger")
    return 0


if __name__ == "__main__":
    sys.exit(main())
