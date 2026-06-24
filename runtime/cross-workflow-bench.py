#!/usr/bin/env python3
"""Cross-workflow cache-accumulation bench — proves the user's long-haul idea: run N
workflows on the SAME codebase from DIFFERENT angles, codebase-FIRST (byte-identical)
+ angle-LAST, and measure whether cache accumulates across workflows toward the floor.

Spawns its own gateway from the CURRENT code. Optionally inserts an IDLE GAP between
workflows to test whether the keep-alive thread keeps the shared prefix warm.

Usage:
  python3 runtime/cross-workflow-bench.py [--workflows 6] [--gap 0] [--keepalive 1]
    --gap N     : sleep N seconds between workflows (simulate a long-haul session)
    --keepalive : 1 = keep-alive ON (default), 0 = OFF (A/B the eviction mitigation)
"""
from __future__ import annotations
import argparse, json, os, signal, subprocess, sys, time, glob, urllib.request
import concurrent.futures as cf
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def find_reasonix() -> str:
    import shutil
    env = os.getenv("REASONIX_BIN")
    if env and os.path.exists(env):
        return env
    onpath = shutil.which("reasonix")
    if onpath:
        return onpath
    home = os.path.expanduser("~")
    for p in sorted(glob.glob(f"{home}/.local/state/fnm_multishells/*/bin/reasonix"), reverse=True):
        if os.path.exists(p):
            return p
    return "reasonix"


def start_gateway(keepalive: bool):
    rx = find_reasonix()
    env = dict(os.environ)
    env["REASONIX_ACP_EPHEMERAL_SESSION"] = "1"
    env["REASONIX_BIN"] = rx
    env["CLAUDE_CODEX_GATEWAY_KEEPALIVE"] = "1" if keepalive else "0"
    env["CLAUDE_CODEX_GATEWAY_KEEPALIVE_INTERVAL_SECONDS"] = "30"
    bd = os.path.dirname(rx)
    if os.path.exists(os.path.join(bd, "node")):
        env["PATH"] = bd + os.pathsep + env["PATH"]
    pf = ROOT / "runtime" / f"xwbench.{os.getpid()}.port"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "reasonix-native-gateway.py"), "--host", "127.0.0.1",
         "--port", "0", "--port-file", str(pf)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    for _ in range(100):
        if pf.exists() and pf.read_text().strip():
            port = int(pf.read_text().strip()); pf.unlink(); return proc, port
        time.sleep(0.1)
    raise SystemExit("gateway did not start")


def lane(port, prompt):
    body = json.dumps({"model": "claude-reasonix-flash", "max_tokens": 40, "stream": True,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages", data=body,
                                 headers={"content-type": "application/json", "x-api-key": "local",
                                          "anthropic-version": "2023-06-01"})
    try:
        for _ in urllib.request.urlopen(req, timeout=180):
            pass
        return 1
    except Exception:
        return 0


def wcache(t0, t1):
    led = ROOT / "runtime" / "reasonix-cost.jsonl"
    rows = [r for r in (json.loads(l) for l in led.read_text().splitlines() if l.strip())
            if t0 - 0.3 <= r.get("ts", 0) <= t1 and isinstance(r.get("cache_pct"), (int, float))
            and r.get("input_tokens", 0) >= 8000]
    tot = sum(r["input_tokens"] for r in rows)
    miss = sum(r["input_tokens"] * (1 - r["cache_pct"] / 100) for r in rows)
    return (100 * (1 - miss / tot) if tot else 0), len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflows", type=int, default=6)
    ap.add_argument("--gap", type=float, default=0.0)
    ap.add_argument("--keepalive", type=int, default=1)
    args = ap.parse_args()

    proc, port = start_gateway(bool(args.keepalive))
    # The shared codebase block — byte-IDENTICAL, placed FIRST in every lane.
    shared = (ROOT / "reasonix-native-gateway.py").read_text(errors="ignore")[:16000]
    SHARED = "Analyze this codebase. SHARED CODEBASE (identical for every lane):\n" + shared + "\n\n"
    results = []
    try:
        for w in range(args.workflows):
            angles = [f"angle {w}-{j}: {kind}" for j, kind in enumerate(
                ["races", "naming", "bugs", "perf", "tests", "docs", "errors", "io"])]
            t0 = time.time()
            with cf.ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(lambda a: lane(port, SHARED + "Now answer from " + a + ". One terse sentence."), angles))
            time.sleep(2)
            cache, n = wcache(t0, time.time())
            results.append(cache)
            print(f"  workflow {w + 1}: cache = {cache:.2f}% ({n} lane)")
            if args.gap > 0 and w < args.workflows - 1:
                time.sleep(args.gap)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    if results:
        first = results[0]
        rest = results[1:]
        avg_rest = sum(rest) / len(rest) if rest else first
        climbed = sum(1 for i in range(1, len(results)) if results[i] >= results[i - 1])
        print(f"\n=== keepalive={'ON' if args.keepalive else 'OFF'} gap={args.gap}s ===")
        print(f"  workflow 1 (cold): {first:.2f}%")
        print(f"  workflows 2..N avg: {avg_rest:.2f}%  (later workflows {'cheaper' if avg_rest > first else 'NOT cheaper'})")
        print(f"  monotonic steps (wf[i]>=wf[i-1]): {climbed}/{len(results)-1}")
        print(f"  min={min(results):.2f}% max={max(results):.2f}%")


if __name__ == "__main__":
    main()
