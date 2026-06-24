#!/usr/bin/env python3
"""The reasonix-fleet MCP must run REASONIX (not the legacy CLI) when CLAUDE_REASONIX_FLAVOR=reasonix.

Root cause this guards: in a claude-reasonix session, single subagents are pushed
to the reasonix_fleet MCP by only-reasonix-fleet.py, but the MCP ran `reasonix exec` = old Reasonix CLI,
so "every agent is reasonix" was false. The MCP must dispatch through reasonix acp
when the session flavor is reasonix.

The test drives the MCP's per-task runner against a fake reasonix binary that
speaks ACP and writes a transcript with a cost record, and asserts the task result
came from reasonix (cost captured), not from a legacy subprocess.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("reasonix_fleet_mcp", ROOT / "reasonix-fleet-mcp.py")
mcp = importlib.util.module_from_spec(spec)
assert spec and spec.loader


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


# Fake reasonix acp binary: speaks the ACP handshake, streams "OK", writes a
# transcript with cost, returns stopReason.
FAKE_REASONIX = r'''#!/usr/bin/env python3
import sys, json
tr=None
a=sys.argv
for i,x in enumerate(a):
    if x=="--transcript" and i+1<len(a): tr=a[i+1]
def w(o): sys.stdout.write(json.dumps(o)+"\n"); sys.stdout.flush()
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    m=json.loads(line); mid=m.get("id"); method=m.get("method")
    if method=="initialize":
        w({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":1,"agentInfo":{"name":"reasonix"}}})
    elif method=="session/new":
        w({"jsonrpc":"2.0","id":mid,"result":{"sessionId":"sess_test"}})
    elif method=="session/prompt":
        w({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"sess_test","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"REASONIX_RAN"}}}})
        if tr:
            with open(tr,"a") as fh:
                fh.write(json.dumps({"role":"assistant_final","content":"REASONIX_RAN","cost":0.000222,"usage":{"prompt_tokens":50,"completion_tokens":3,"prompt_cache_hit_tokens":45,"prompt_cache_miss_tokens":5}})+"\n")
        w({"jsonrpc":"2.0","id":mid,"result":{"stopReason":"end_turn","transcriptPath":tr}})
'''


def make_fake_reasonix():
    d = tempfile.mkdtemp()
    p = Path(d) / "reasonix"
    p.write_text(FAKE_REASONIX, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_mcp_runs_reasonix_in_reasonix_flavor():
    fake = make_fake_reasonix()
    os.environ["CLAUDE_REASONIX_FLAVOR"] = "reasonix"
    os.environ["REASONIX_BIN"] = fake
    try:
        spec.loader.exec_module(mcp)  # load with reasonix env set
        cwd = tempfile.mkdtemp()
        task = {"title": "t", "prompt": "say OK", "cwd": cwd}
        result = asyncio.run(mcp.run_one_task(task, 0, "batch-test", 8000))
        expect(result.get("ok") is True, f"task should succeed: {result}")
        out = str(result.get("output") or result.get("stdout") or "")
        expect("REASONIX_RAN" in out, f"output must come from reasonix engine: {result}")
        # cost from reasonix must be surfaced
        expect(result.get("reasonix_cost_usd") == 0.000222,
               f"reasonix cost must be captured: {result}")
    finally:
        os.environ.pop("CLAUDE_REASONIX_FLAVOR", None)
        os.environ.pop("REASONIX_BIN", None)


def main() -> int:
    test_mcp_runs_reasonix_in_reasonix_flavor()
    print("PASS: mcp reasonix flavor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
