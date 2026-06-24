#!/usr/bin/env python3
"""Unit tests for run_reasonix_acp using a fake `reasonix` binary that speaks ACP."""
from __future__ import annotations
import importlib.util, json, os, stat, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("rx_gateway", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(gw)

def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")

# A fake `reasonix` that: reads NDJSON requests, answers initialize + session/new,
# streams two agent_message_chunk updates ("PO","NG"), then returns stopReason.
# Like real `reasonix acp`, it writes per-turn usage+cost to the --transcript
# JSONL file (NOT to stderr — acp mode does not print a cost line on stderr).
FAKE = r'''#!/usr/bin/env python3
import sys, json
def w(o): sys.stdout.write(json.dumps(o)+"\n"); sys.stdout.flush()
# parse --transcript <path> from argv (real acp takes this flag)
tr=None
a=sys.argv
for i,x in enumerate(a):
    if x=="--transcript" and i+1<len(a): tr=a[i+1]
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
        if tr:
            with open(tr,"a") as fh:
                fh.write(json.dumps({"turn":1,"role":"assistant_final","content":"PONG","cost":0.000123,"usage":{"prompt_tokens":100,"completion_tokens":4,"total_tokens":104,"prompt_cache_hit_tokens":90,"prompt_cache_miss_tokens":10}})+"\n")
        w({"jsonrpc":"2.0","id":mid,"result":{"stopReason":"end_turn","transcriptPath":tr}})
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
    # cost + cache come from the --transcript JSONL (acp mode), not stderr.
    expect(usage.get("reasonix_cost_usd") == 0.000123, f"cost not captured from transcript: {usage}")
    expect(usage.get("reasonix_cache_pct") == 90.0, f"cache pct (90 hit / 10 miss) not computed: {usage}")
    # token counts come from the real transcript usage, not an estimate.
    expect(usage.get("input_tokens") == 100, f"input_tokens should be real transcript value: {usage}")
    expect(usage.get("output_tokens") == 4, f"output_tokens should be real transcript value: {usage}")

def test_spawn_failure_raises_gatewayerror():
    cfg = {"reasonix_bin": "/nonexistent/reasonix-binary-xyz", "target_model": "deepseek-v4-flash"}
    try:
        gw.run_reasonix_acp("hi", cfg)
    except gw.GatewayError as e:
        expect(e.error_type == "reasonix_acp_error",
               f"wrong error_type: {e.error_type!r}")
        return
    except Exception as e:
        raise SystemExit(f"FAIL: expected GatewayError, got {type(e).__name__}: {e}")
    raise SystemExit("FAIL: no exception raised — expected GatewayError")

def test_registry_has_reasonix_flash():
    os.environ["CLAUDE_REASONIX_FLAVOR"] = "reasonix"
    try:
        reg = gw.model_registry()
        expect("claude-reasonix-flash" in reg, f"registry missing claude-reasonix-flash: {list(reg)}")
        cfg = reg["claude-reasonix-flash"]
        expect(cfg.get("provider") == "reasonix_cli", f"wrong provider: {cfg}")
        expect(cfg.get("target_model") == "deepseek-v4-flash", f"wrong model: {cfg}")
    finally:
        os.environ.pop("CLAUDE_REASONIX_FLAVOR", None)

def main():
    test_accumulates_text_and_cost()
    test_spawn_failure_raises_gatewayerror()
    test_registry_has_reasonix_flash()
    print("PASS: reasonix acp driver")
    return 0

if __name__ == "__main__":
    sys.exit(main())
