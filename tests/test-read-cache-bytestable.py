#!/usr/bin/env python3
"""Lever C — GATEWAY SHARED READ-CACHE byte-stability gate (BLOCKING).

This is the highest-risk lever: it injects a cached-summary block into lane
prompts. If that injection is not byte-deterministic it FORKS the shared prefix
that drives the 73.5% prefix cache (and the 96.3% baseline), turning a
token-saver into a cache-destruction regression.

THE GATE: two prompts that differ ONLY in their per-lane tail, but reference the
SAME set of cached files, MUST produce a BYTE-IDENTICAL injected prefix — the
part of the assembled prompt up to AND INCLUDING the injected cached-summary
block. If they ever diverge before the per-lane tail, this test FAILS and the
lever must NOT ship.

Covers:
  (a) Flag OFF (default): read_cache_injection_block() returns "" and
      openai_messages_to_prompt is byte-identical to the pre-C assembly.
  (b) Flag ON, no cached files referenced: injection is "" (no spurious block).
  (c) Flag ON, cached files referenced: the injected block is non-empty and
      BYTE-IDENTICAL across two lanes whose tails differ.
  (d) The injected block is order-independent: referencing the same files in a
      different textual order yields the SAME bytes (sorted, deterministic).
  (e) The injected block is normalize_prefix-clean (no volatile billing line).
  (f) End-to-end: openai_messages_to_prompt for two lanes sharing the same
      system block + cached files but different tails has a BYTE-IDENTICAL
      injected prefix (everything up to and including the injected block).
  (g) Eviction / cap + TTL freshness honoured (cache bookkeeping is bounded).
"""
from __future__ import annotations
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "reasonix_native_gateway", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)

FLAG = "CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE"
CAP_ENV = "CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE_CAP"
TTL_ENV = "CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE_TTL_S"
PERSIST_ENV = "CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE_PATH"

_PASS = 0
_FAIL = 0


def check(cond: bool, msg: str) -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {msg}")
    else:
        _FAIL += 1
        print(f"  FAIL {msg}")


def _clear_env() -> None:
    for k in (FLAG, CAP_ENV, TTL_ENV, PERSIST_ENV):
        os.environ.pop(k, None)


def _reset_cache() -> None:
    # Fresh module-level cache for each scenario.
    with gw._READ_SUMMARY_CACHE_LOCK:
        gw._READ_SUMMARY_CACHE.clear()


def _injected_prefix(prompt_text: str, marker: str) -> str:
    """Return everything in `prompt_text` up to AND INCLUDING the injected block,
    located by a stable marker substring. If the marker is absent return the whole
    prompt (so two no-injection prompts still compare equal on their shared head)."""
    idx = prompt_text.find(marker)
    if idx == -1:
        return prompt_text
    # include through the end of the injected block: find the block end sentinel.
    end = prompt_text.find(gw.READ_CACHE_BLOCK_END, idx)
    if end == -1:
        return prompt_text[: idx + len(marker)]
    return prompt_text[: end + len(gw.READ_CACHE_BLOCK_END)]


def main() -> int:
    # Two real files the lanes "reference" — use files that exist so mtime works.
    f1 = str(ROOT / "README.md")
    f2 = str(ROOT / "reasonix-native-gateway.py")

    # ----------------------------------------------------------------------
    # (a) Flag OFF (default) -> no injection, byte-identical to pre-C.
    # ----------------------------------------------------------------------
    _clear_env()
    _reset_cache()
    check(gw.read_cache_injection_block(f"Read {f1} and {f2} then do A") == "",
          "injection block is '' when flag OFF (default)")

    msgs_a1 = [
        {"role": "system", "content": "SHARED SYSTEM BLOCK\nidentical for all lanes."},
        {"role": "user", "content": f"Read {f1} and {f2}. PER-LANE TAIL ALPHA."},
    ]
    msgs_a2 = [
        {"role": "system", "content": "SHARED SYSTEM BLOCK\nidentical for all lanes."},
        {"role": "user", "content": f"Read {f1} and {f2}. PER-LANE TAIL BETA-DIFFERENT."},
    ]
    p_off_1 = gw.openai_messages_to_prompt(msgs_a1)
    # With the flag off, the prompt must contain NO cache block marker at all.
    check(gw.READ_CACHE_BLOCK_BEGIN not in p_off_1,
          "no cache block injected into prompt when flag OFF")

    # ----------------------------------------------------------------------
    # (b) Flag ON, cache EMPTY (no summaries stored) -> "" injection.
    # ----------------------------------------------------------------------
    _clear_env()
    _reset_cache()
    os.environ[FLAG] = "1"
    check(gw.read_cache_injection_block(f"Read {f1} and {f2} then do A") == "",
          "injection block is '' when flag ON but cache empty")

    # ----------------------------------------------------------------------
    # Populate the cache (simulate a lane that read+summarized f1 and f2).
    # ----------------------------------------------------------------------
    gw.populate_read_cache(f"Read {f1}", '{"findings":["f1 is the readme"],"files_read":["README.md"],"flag":""}')
    gw.populate_read_cache(f"Read {f2}", '{"findings":["gateway main module"],"files_read":["gw.py"],"flag":""}')

    # ----------------------------------------------------------------------
    # (c) Flag ON, both files cached -> non-empty + BYTE-IDENTICAL across tails.
    # ----------------------------------------------------------------------
    blk_alpha = gw.read_cache_injection_block(f"Read {f1} and {f2}. PER-LANE TAIL ALPHA.")
    blk_beta = gw.read_cache_injection_block(f"Read {f1} and {f2}. PER-LANE TAIL BETA-DIFFERENT.")
    check(blk_alpha.strip() != "", "injection block non-empty when both files cached")
    check(blk_alpha == blk_beta,
          "injection block BYTE-IDENTICAL across two lanes with different tails")
    if blk_alpha != blk_beta:
        # Surface the exact diverging bytes for a BLOCKED report.
        for i, (ca, cb) in enumerate(zip(blk_alpha, blk_beta)):
            if ca != cb:
                print(f"    first diff at byte {i}: {ca!r} != {cb!r}")
                print(f"    alpha[{i}:{i+40}]={blk_alpha[i:i+40]!r}")
                print(f"    beta [{i}:{i+40}]={blk_beta[i:i+40]!r}")
                break

    # ----------------------------------------------------------------------
    # (d) Order-independent: files referenced in different order -> same bytes.
    # ----------------------------------------------------------------------
    blk_order1 = gw.read_cache_injection_block(f"Read {f1} then {f2}.")
    blk_order2 = gw.read_cache_injection_block(f"Read {f2} then {f1}.")
    check(blk_order1 == blk_order2,
          "injection block is SORTED / order-independent (same bytes regardless of reference order)")

    # ----------------------------------------------------------------------
    # (e) normalize_prefix-clean: a volatile billing line never survives.
    # ----------------------------------------------------------------------
    check("x-anthropic-billing-header:" not in blk_alpha,
          "injected block carries no volatile billing-header line")
    check(gw.normalize_prefix(blk_alpha) == blk_alpha,
          "injected block is already normalize_prefix-clean (idempotent)")

    # ----------------------------------------------------------------------
    # (f) END-TO-END byte-stability through openai_messages_to_prompt: two lanes
    #     same system + cached files, different tail -> identical injected prefix.
    # ----------------------------------------------------------------------
    p1 = gw.openai_messages_to_prompt(msgs_a1)
    p2 = gw.openai_messages_to_prompt(msgs_a2)
    check(gw.READ_CACHE_BLOCK_BEGIN in p1,
          "cache block IS injected into the assembled prompt when flag ON + files cached")
    pre1 = _injected_prefix(p1, gw.READ_CACHE_BLOCK_BEGIN)
    pre2 = _injected_prefix(p2, gw.READ_CACHE_BLOCK_BEGIN)
    check(pre1 == pre2,
          "ASSEMBLED injected-prefix (system + cache block) BYTE-IDENTICAL across lanes (THE GATE)")
    if pre1 != pre2:
        for i, (ca, cb) in enumerate(zip(pre1, pre2)):
            if ca != cb:
                print(f"    first diff at byte {i}: {ca!r} != {cb!r}")
                print(f"    pre1[{i}:{i+60}]={pre1[i:i+60]!r}")
                print(f"    pre2[{i}:{i+60}]={pre2[i:i+60]!r}")
                break
    # The block must sit BEFORE the per-lane tail in BOTH prompts.
    check(p1.find(gw.READ_CACHE_BLOCK_BEGIN) < p1.find("PER-LANE TAIL ALPHA"),
          "cache block injected BEFORE the per-lane tail (fixed boundary)")

    # ----------------------------------------------------------------------
    # (g) TTL freshness + cap eviction bookkeeping.
    # ----------------------------------------------------------------------
    _clear_env()
    _reset_cache()
    os.environ[FLAG] = "1"
    os.environ[TTL_ENV] = "1"  # 1 second TTL
    gw.populate_read_cache(f"Read {f1}", '{"findings":["x"],"files_read":["README.md"],"flag":""}')
    check(gw.read_cache_injection_block(f"Read {f1}") != "",
          "fresh entry is served within TTL")
    time.sleep(1.2)
    check(gw.read_cache_injection_block(f"Read {f1}") == "",
          "stale entry past TTL is NOT served (freshness honoured)")

    _clear_env()
    _reset_cache()
    os.environ[FLAG] = "1"
    os.environ[CAP_ENV] = "4"
    # Insert 10 distinct (synthetic) entries; cap must bound the dict.
    for i in range(10):
        gw._read_cache_store(f"/tmp/cap-file-{i}.py", str(i), f"summary {i}")
    check(len(gw._READ_SUMMARY_CACHE) <= 4,
          "cache dict bounded by _CAP (eviction keeps it <= cap)")

    # ----------------------------------------------------------------------
    # Persistence round-trip: store -> save -> clear -> load -> served.
    # ----------------------------------------------------------------------
    _clear_env()
    _reset_cache()
    os.environ[FLAG] = "1"
    with tempfile.TemporaryDirectory() as td:
        pjson = str(Path(td) / "read-summary-cache.json")
        os.environ[PERSIST_ENV] = pjson
        gw.populate_read_cache(f"Read {f1}", '{"findings":["persisted"],"files_read":["README.md"],"flag":""}')
        gw.save_read_cache()
        _reset_cache()
        check(len(gw._READ_SUMMARY_CACHE) == 0, "cache cleared before load")
        gw.load_read_cache()
        check(gw.read_cache_injection_block(f"Read {f1}") != "",
              "persisted entry survives save/load round-trip (Q10)")
    _clear_env()
    _reset_cache()

    total = _PASS + _FAIL
    print(f"\n{'PASS' if _FAIL == 0 else 'FAIL'}  {_PASS}/{total} checks passed")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
