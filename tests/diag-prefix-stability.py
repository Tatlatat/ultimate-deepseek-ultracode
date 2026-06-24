#!/usr/bin/env python3
"""Diagnostic (NOT a unit test): is reasonix acp's immutable prefix byte-stable
across N fresh sequential spawns with an identical prompt + identical --dir?

It spawns reasonix acp N times (each a fresh `session/new`), sends the SAME
short prompt, and reads from each run's --transcript JSONL the per-turn
diagnostic that reasonix already records:
  prefixHash / systemHash / toolSpecsHash / prompt_tokens / cache hit-miss.

If prefixHash is identical across runs -> the prefix is byte-stable and any
DeepSeek server-side prefix cache CAN hit across spawns. If it varies, the
sub-hashes say WHICH component (system / tools / fewShots) drifted.

This is ground-truth capture for the 99.2% goal: no guessing about what
reasonix injects — we read its own fingerprint of what it sent.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

REASONIX_BIN = os.environ.get("REASONIX_BIN") or shutil.which("reasonix") or "reasonix"
# Optional: a node dir to prepend to PATH (the gateway needs node alongside reasonix).
# Defaults empty; set DIAG_NODE_DIR if reasonix's node is not already on PATH.
NODE_DIR = os.environ.get("DIAG_NODE_DIR", os.path.dirname(REASONIX_BIN) if os.path.sep in REASONIX_BIN else "")
MODEL = os.environ.get("DIAG_MODEL", "deepseek-chat")
EFFORT = os.environ.get("DIAG_EFFORT", "low")
PROMPT = os.environ.get("DIAG_PROMPT", "Reply with exactly the single word: PONG")
N = int(os.environ.get("DIAG_N", "3"))
TIMEOUT = float(os.environ.get("DIAG_TIMEOUT", "120"))


def run_once(workdir: str, transcript_path: str) -> dict:
    env = dict(os.environ)
    env["PATH"] = NODE_DIR + os.pathsep + env.get("PATH", "")
    cmd = [
        REASONIX_BIN, "acp",
        "--dir", workdir,
        "--yolo",
        "-m", MODEL,
        "--effort", EFFORT,
        "--transcript", transcript_path,
    ]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1, cwd=workdir, env=env,
    )
    out_q: "queue.Queue[dict]" = queue.Queue()

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                out_q.put(json.loads(line))
            except Exception:
                pass
        out_q.put({"__eof__": True})

    threading.Thread(target=reader, daemon=True).start()

    def send(obj: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": 1, "clientCapabilities": {}}})
    send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
          "params": {"cwd": workdir, "mcpServers": []}})

    session_id = None
    prompted = False
    done = False
    deadline = time.monotonic() + TIMEOUT
    while not done and time.monotonic() < deadline:
        try:
            msg = out_q.get(timeout=1.0)
        except queue.Empty:
            continue
        if msg.get("__eof__"):
            break
        if msg.get("id") == 2 and "result" in msg:
            session_id = msg["result"].get("sessionId")
            if session_id and not prompted:
                prompted = True
                send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                      "params": {"sessionId": session_id,
                                 "prompt": [{"type": "text", "text": PROMPT}]}})
        if msg.get("id") == 3 and ("result" in msg or "error" in msg):
            done = True

    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except Exception:
        proc.kill()

    # Read the transcript: find the assistant_final record with prefixHash + usage.
    rec_out = {"prefixHash": None, "systemHash": None, "toolSpecsHash": None,
               "fewShotsHash": None, "toolCount": None, "prompt_tokens": None,
               "hit": None, "miss": None, "cache_pct": None, "missReason": None}
    try:
        with open(transcript_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("prefixHash"):
                    rec_out["prefixHash"] = rec["prefixHash"]
                cd = rec.get("cacheDiagnostic")
                if isinstance(cd, dict):
                    rec_out["systemHash"] = cd.get("systemHash")
                    rec_out["toolSpecsHash"] = cd.get("toolSpecsHash")
                    rec_out["fewShotsHash"] = cd.get("fewShotsHash")
                    rec_out["toolCount"] = cd.get("toolCount")
                    rec_out["missReason"] = cd.get("missReason")
                u = rec.get("usage")
                if isinstance(u, dict):
                    if isinstance(u.get("prompt_tokens"), int):
                        rec_out["prompt_tokens"] = u["prompt_tokens"]
                    h = u.get("prompt_cache_hit_tokens")
                    m = u.get("prompt_cache_miss_tokens")
                    if isinstance(h, int) and isinstance(m, int) and (h + m) > 0:
                        rec_out["hit"] = h
                        rec_out["miss"] = m
                        rec_out["cache_pct"] = round(100.0 * h / (h + m), 1)
    except Exception as exc:
        rec_out["_transcript_error"] = str(exc)
    return rec_out


def main() -> int:
    base = tempfile.mkdtemp(prefix="reasonix-diag-")
    # Fixed empty workdir reused for ALL runs -> cwd context is byte-identical.
    workdir = os.path.join(base, "wd")
    os.makedirs(workdir, exist_ok=True)
    print(f"workdir (fixed, empty): {workdir}")
    print(f"reasonix: {REASONIX_BIN}  model={MODEL} effort={EFFORT}")
    print(f"prompt: {PROMPT!r}   N={N}\n")

    rows = []
    for i in range(N):
        tpath = os.path.join(base, f"t{i}.jsonl")
        t0 = time.monotonic()
        r = run_once(workdir, tpath)
        r["_secs"] = round(time.monotonic() - t0, 1)
        rows.append(r)
        print(f"run {i}: prefixHash={r['prefixHash']}  systemHash={r['systemHash']}  "
              f"toolSpecsHash={r['toolSpecsHash']}  tools={r['toolCount']}  "
              f"in_tok={r['prompt_tokens']}  cache={r['cache_pct']}%  "
              f"miss={r['miss']}  reason={r['missReason']}  ({r['_secs']}s)")

    print("\n=== VERDICT ===")
    pfx = [r["prefixHash"] for r in rows if r["prefixHash"]]
    sysh = [r["systemHash"] for r in rows if r["systemHash"]]
    toolh = [r["toolSpecsHash"] for r in rows if r["toolSpecsHash"]]
    toks = [r["prompt_tokens"] for r in rows if r["prompt_tokens"]]
    print(f"unique prefixHash : {len(set(pfx))}  -> {sorted(set(pfx))}")
    print(f"unique systemHash : {len(set(sysh))}  -> {sorted(set(sysh))}")
    print(f"unique toolSpecsHash: {len(set(toolh))} -> {sorted(set(toolh))}")
    print(f"prompt_tokens     : {toks}  (spread={max(toks)-min(toks) if toks else 'n/a'})")
    if pfx and len(set(pfx)) == 1:
        print("PREFIX IS BYTE-STABLE across fresh spawns. Server-side prefix cache CAN hit.")
    elif pfx:
        print("PREFIX DRIFTS across spawns -> sub-hashes above pinpoint the component.")
    else:
        print("NO prefixHash captured -> transcript schema differs (installed reasonix may predate the diagnostic).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
