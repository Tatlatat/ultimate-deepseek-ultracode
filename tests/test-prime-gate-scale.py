#!/usr/bin/env python3
"""[FLAWED — DO NOT TRUST THE NUMBER] reuses one cwd so reasonix accumulates
session history (in_tok grows ~3.5K/lane), confounding cache%. The gate ON-vs-OFF
DELTA is still directional (50%->86%) but absolute cache is wrong. The trustworthy
signal is the REAL 3-lane Opus run (prefix-trace.opus-allfix-tiny.jsonl): followers 99.9%.

Direct prime-gate-at-scale test — bypasses the Opus controller entirely.

Fires N lanes concurrently through the gateway's run_reasonix_acp (exactly the
path a real UltraCode fan-out uses), all sharing ONE byte-identical shared
prefix + a tiny per-lane unique suffix. Measures weighted cache to confirm the
prefix-prime gate lifts a real burst to >=99.2%. No Anthropic/Opus call at all.
"""
from __future__ import annotations
import concurrent.futures as cf
import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)

N = int(os.environ.get("SCALE_N", "24"))
# A realistic shared review block (~12KB) — same bytes for every lane.
SHARED = (
    "SYSTEM:\nYou are Claude Code, Anthropic's official CLI for Claude.\n"
    "You are a native subagent reviewing code for cache-stability.\n\n"
    "SHARED REVIEW CONTEXT (identical for every lane):\n"
    + ("// runtime.ts and prompt.ts under review. " * 400)
    + "\n\nReply with one short word.\n"
)
CONFIG = {
    "provider": "reasonix_cli",
    "target_model": os.environ.get("CLAUDE_REASONIX_REASONIX_MODEL", "deepseek-v4-flash"),
    "reasonix_bin": os.environ.get(
        "REASONIX_BIN",
        "/Users/tatlatat/.local/state/fnm_multishells/99956_1781810966752/bin/reasonix",
    ),
}


import tempfile

# One fixed EMPTY workdir for all lanes, set once (not per-lane: env mutation under
# concurrency is racy). Reasonix gives each acp spawn a fresh timestamped session,
# so an empty cwd means no on-disk session to accumulate — this removes the
# "in_tok grows per lane" confound that came from a cwd holding a prior session.
_WORKDIR = tempfile.mkdtemp(prefix="primegate-scale-")
os.environ["CLAUDE_REASONIX_GATEWAY_CODEX_CWD"] = _WORKDIR


def lane(i: int) -> dict:
    # Shared block FIRST (byte-identical), tiny unique suffix LAST.
    prompt = SHARED + f"\nLANE-{i} dimension."
    t0 = time.monotonic()
    try:
        _text, usage = gw.run_reasonix_acp(prompt, CONFIG)
        return {
            "i": i,
            "in_tok": usage.get("input_tokens") or usage.get("prompt_tokens"),
            "cache": usage.get("reasonix_cache_pct"),
            "secs": round(time.monotonic() - t0, 1),
        }
    except Exception as exc:  # noqa: BLE001
        return {"i": i, "error": str(exc)[:80], "secs": round(time.monotonic() - t0, 1)}


def main() -> int:
    print(f"firing {N} concurrent lanes, shared prefix ~{len(SHARED)} chars")
    print(f"PRIME_GATE={os.environ.get('CLAUDE_REASONIX_GATEWAY_PRIME_GATE', '1(default)')}")
    rows = []
    with cf.ThreadPoolExecutor(max_workers=N) as ex:
        futs = [ex.submit(lane, i) for i in range(N)]
        for f in cf.as_completed(futs):
            r = f.result()
            rows.append(r)
            tag = r.get("error") or f"cache={r['cache']}% in_tok={r['in_tok']}"
            print(f"  lane {r['i']:>2}: {tag} ({r['secs']}s)")

    ok = [r for r in rows if isinstance(r.get("cache"), (int, float)) and r.get("in_tok")]
    if not ok:
        print("NO usable lanes (all errored).")
        return 1
    tot_in = sum(r["in_tok"] for r in ok)
    tot_miss = sum(r["in_tok"] * (1 - r["cache"] / 100) for r in ok)
    w = 100 * (1 - tot_miss / tot_in)
    cps = [r["cache"] for r in ok]
    print(f"\n=== {len(ok)}/{N} lanes ===")
    print(f"weighted cache = {w:.2f}%   min={min(cps)}%  max={max(cps)}%  mean={sum(cps)/len(cps):.2f}%")
    print(f"TARGET 99.2% -> {'*** MET ***' if w >= 99.2 else 'NOT MET (gap %.2f)' % (99.2 - w)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
