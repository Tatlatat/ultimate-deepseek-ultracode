#!/usr/bin/env python3
"""Realworld automated benchmark — ONE command that runs the THREE things you
actually use claude-reasonix for, end-to-end against a freshly-spawned gateway
(loading the CURRENT code), and grades QUALITY first, then cost/speed.

  Scenario A — single subagent: one reasonix lane does a concrete file task.
  Scenario B — UltraCode fan-out: N small lanes in parallel, each one file.
  Scenario C — read-then-synthesize: read lanes (prose) + one synthesize lane.

For EACH lane it records what the user actually fears — not just cache %:
  - empty        : lane came back with no real content (hollow lane)
  - errored      : lane raised / timed out
  - too_slow     : lane wall-clock over a threshold
  - cache_pct / in_tok : cost signals
And prints a PASS/FAIL verdict per quality gate plus weighted cache.

Run:  python3 runtime/realworld-bench.py            # full run, auto gateway
      python3 runtime/realworld-bench.py --json      # machine-readable summary
Self-contained: spawns its own gateway on a random port from THIS source file,
uses the real reasonix CLI + DeepSeek, and tears the gateway down at the end. It
does NOT touch any gateway you already have running.
"""
from __future__ import annotations
import argparse, json, os, re, signal, socket, subprocess, sys, time, urllib.request
import concurrent.futures as cf
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATEWAY = ROOT / "codex-native-gateway.py"
SLOW_SECS = float(os.getenv("REALWORLD_SLOW_SECS", "90"))


def find_reasonix() -> str:
    env = os.getenv("REASONIX_BIN")
    if env and os.path.exists(env):
        return env
    import glob
    for p in sorted(glob.glob("/Users/tatlatat/.local/state/fnm_multishells/*/bin/reasonix"), reverse=True):
        if os.path.exists(p):
            return p
    return "reasonix"


def start_gateway() -> tuple[subprocess.Popen, int]:
    portfile = ROOT / "runtime" / f"realworld-gw.{os.getpid()}.port"
    if portfile.exists():
        portfile.unlink()
    env = dict(os.environ)
    env.setdefault("REASONIX_ACP_EPHEMERAL_SESSION", "1")  # the session-isolation fix
    env["CLAUDE_CODEX_GATEWAY_REASONIX_EPHEMERAL"] = "1"
    rx = find_reasonix()
    env["REASONIX_BIN"] = rx
    bindir = os.path.dirname(rx)
    if os.path.exists(os.path.join(bindir, "node")):
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    proc = subprocess.Popen(
        [sys.executable, str(GATEWAY), "--host", "127.0.0.1", "--port", "0",
         "--port-file", str(portfile)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    for _ in range(100):
        if portfile.exists() and portfile.read_text().strip():
            port = int(portfile.read_text().strip())
            portfile.unlink()
            return proc, port
        if proc.poll() is not None:
            raise SystemExit("gateway died on startup")
        time.sleep(0.1)
    raise SystemExit("gateway did not report a port")


def lane(port: int, prompt: str, schema: dict | None = None) -> dict:
    body = {"model": "claude-reasonix-flash", "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}]}
    if schema:
        body["tools"] = [{"name": "StructuredOutput", "input_schema": schema}]
        body["tool_choice"] = {"type": "tool", "name": "StructuredOutput"}
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages", data=data,
        headers={"content-type": "application/json", "x-api-key": "local",
                 "anthropic-version": "2023-06-01"})
    t0 = time.monotonic()
    text, tool_input, in_tok, cache = "", None, 0, None
    try:
        raw = urllib.request.urlopen(req, timeout=600).read().decode("utf-8", "ignore")
    except Exception as e:
        return {"errored": True, "err": str(e)[:80], "secs": round(time.monotonic() - t0, 1)}
    partial = {}
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except Exception:
            continue
        d = ev.get("delta", {})
        if d.get("type") == "text_delta":
            text += d.get("text", "")
        elif d.get("type") == "input_json_delta":
            partial[ev.get("index", 0)] = partial.get(ev.get("index", 0), "") + d.get("partial_json", "")
        u = ev.get("usage") or (ev.get("message", {}) or {}).get("usage")
        if u and u.get("input_tokens"):
            in_tok = u["input_tokens"]
    if partial:
        try:
            tool_input = json.loads(next(iter(partial.values())))
        except Exception:
            tool_input = None
    secs = round(time.monotonic() - t0, 1)
    has_content = bool(tool_input) or bool(text.strip())
    hollow_marker = "returned no content" in text  # the hollow-lane guard text
    return {
        "errored": False, "secs": secs, "in_tok": in_tok, "cache_pct": cache,
        "empty": (not has_content) or hollow_marker,
        "too_slow": secs > SLOW_SECS,
        "text_head": text[:80], "tool_input": tool_input,
    }


def ledger_window(t0: float, t1: float) -> dict:
    led = ROOT / "runtime" / "reasonix-cost.jsonl"
    rows = []
    if led.exists():
        for l in led.read_text(errors="ignore").splitlines():
            try:
                r = json.loads(l)
            except Exception:
                continue
            if t0 <= r.get("ts", 0) <= t1 and isinstance(r.get("cache_pct"), (int, float)) \
                    and r.get("input_tokens", 0) >= 500:
                rows.append(r)
    if not rows:
        return {"lanes": 0, "weighted": None}
    tot = sum(r["input_tokens"] for r in rows)
    miss = sum(r["input_tokens"] * (1 - r["cache_pct"] / 100) for r in rows)
    return {"lanes": len(rows), "weighted": round(100 * (1 - miss / tot), 2),
            "min_cache": min(r["cache_pct"] for r in rows)}


def run_all(port: int) -> dict:
    t0 = time.time()
    out = {}

    # A — single subagent doing a concrete, checkable file task.
    a = lane(port, "In one sentence, what does the file "
             f"{ROOT}/system-prompt-reasonix.md say a Reasonix lane is strongest at? "
             "Read just that file.")
    out["A_single"] = [a]

    # B — UltraCode-style fan-out: N small lanes, each one file, concurrently.
    files = ["codex-native-gateway.py", "hooks/codex-workflow.py",
             "system-prompt-reasonix.md", "README.md", "hooks/only-codex-fleet.py"]
    prompts = [f"Read ONLY {ROOT}/{f} and summarize its purpose in one sentence." for f in files]
    with cf.ThreadPoolExecutor(max_workers=len(prompts)) as ex:
        out["B_fanout"] = list(ex.map(lambda p: lane(port, p), prompts))

    # C — read-then-synthesize (deep-research shape, structured synthesize lane).
    SYN = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}
    notes = [r.get("text_head", "") for r in out["B_fanout"] if not r.get("errored")]
    c = lane(port, "Synthesize and merge these notes into one short summary object.\n\n"
             + "\n".join(f"- {n}" for n in notes), schema=SYN)
    out["C_synth"] = [c]

    time.sleep(2)
    out["_ledger"] = ledger_window(t0 - 1, time.time())
    return out


def grade(out: dict) -> dict:
    lanes = [r for k, v in out.items() if not k.startswith("_") for r in v]
    n = len(lanes)
    errored = [r for r in lanes if r.get("errored")]
    empty = [r for r in lanes if not r.get("errored") and r.get("empty")]
    slow = [r for r in lanes if not r.get("errored") and r.get("too_slow")]
    led = out.get("_ledger", {})
    gates = {
        "no_errored": (len(errored) == 0, f"{len(errored)}/{n} lanes errored"),
        "no_empty": (len(empty) == 0, f"{len(empty)}/{n} lanes empty/hollow"),
        "no_too_slow": (len(slow) == 0, f"{len(slow)}/{n} lanes >{int(SLOW_SECS)}s"),
        "cache_ge_99_2": ((led.get("weighted") or 0) >= 99.2,
                          f"weighted cache {led.get('weighted')}% (target 99.2)"),
    }
    passed = all(ok for ok, _ in gates.values())
    return {"passed": passed, "gates": gates, "n_lanes": n, "ledger": led}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    proc, port = start_gateway()
    try:
        out = run_all(port)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    result = grade(out)
    if args.json:
        print(json.dumps({"result": result, "detail": out}, indent=2))
        return
    print(f"=== REALWORLD BENCH — {result['n_lanes']} lanes (real reasonix+DeepSeek) ===")
    for name, (ok, msg) in result["gates"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {msg}")
    print(f"\nVERDICT: {'*** ALL GATES PASS ***' if result['passed'] else 'NOT YET — see FAIL gates'}")
    # show the worst lanes for context
    bad = [r for k, v in out.items() if not k.startswith("_") for r in v
           if r.get("errored") or r.get("empty") or r.get("too_slow")]
    if bad:
        print("\nproblem lanes:")
        for r in bad[:6]:
            tag = r.get("err") or ("empty" if r.get("empty") else "slow")
            print(f"  - {tag} ({r.get('secs')}s) {r.get('text_head', '')[:50]}")


if __name__ == "__main__":
    main()
