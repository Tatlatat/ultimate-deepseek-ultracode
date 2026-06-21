#!/usr/bin/env python3
"""Unit tests for run_reasonix_acp using a fake `reasonix` binary that speaks ACP."""
from __future__ import annotations
import importlib.util, json, os, stat, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("rx_gateway", ROOT / "codex-native-gateway.py")
gw = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(gw)

def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")

# A fake `reasonix` that: reads NDJSON requests, answers initialize + session/new,
# streams two agent_message_chunk updates ("PO","NG"), then returns stopReason and
# prints the reasonix cost line on stderr.
FAKE = r'''#!/usr/bin/env python3
import sys, json
def w(o): sys.stdout.write(json.dumps(o)+"\n"); sys.stdout.flush()
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    m=json.loads(line)
    mid=m.get("id"); method=m.get("method")
    if method=="initialize":
        w({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":1,"agentInfo":{"name":"reasonix"}}})
    elif method=="session/new":
        w({"jsonrpc":"2.0","id":mid,"result":{"sessionId":"sess_test"}})
    elif method=="session/prompt":
        for piece in ("PO","NG"):
            w({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"sess_test","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":piece}}}})
        sys.stderr.write("— turns:1 cache:90.0% cost:$0.000123 save-vs-claude:99.0%\n"); sys.stderr.flush()
        w({"jsonrpc":"2.0","id":mid,"result":{"stopReason":"end_turn","transcriptPath":None}})
'''

def make_fake():
    d = tempfile.mkdtemp()
    p = Path(d) / "reasonix"
    p.write_text(FAKE, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)

def test_accumulates_text_and_cost():
    fake = make_fake()
    cfg = {"reasonix_bin": fake, "target_model": "deepseek-v4-flash"}
    text, usage = gw.run_reasonix_acp("say PONG", cfg)
    expect(text == "PONG", f"expected accumulated 'PONG', got {text!r}")
    expect(usage.get("reasonix_cost_usd") == 0.000123, f"cost not captured: {usage}")
    expect(usage.get("reasonix_cache_pct") == 90.0, f"cache pct not captured: {usage}")

def main():
    test_accumulates_text_and_cost()
    print("PASS: reasonix acp driver")
    return 0

if __name__ == "__main__":
    sys.exit(main())
