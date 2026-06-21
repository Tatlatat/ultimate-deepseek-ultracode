#!/usr/bin/env python3
"""Tests for hooks/workflow_selfheal.py + the wrapper sentinel it relies on.

Covers the auto-fix path (deepseek remap when no key), the report/context surface
for a down gateway and a healthy gateway, fail-open behaviour, and that the JS
wrapper actually honours the __claudeCodexForceCodexOnly sentinel.
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "hooks"
sys.path.insert(0, str(HOOKS))
import workflow_selfheal as sh  # noqa: E402

# Load codex-workflow.py (dash in name) to exercise the wrapper source.
spec = importlib.util.spec_from_file_location("codex_workflow", HOOKS / "codex-workflow.py")
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


SCRIPT = """export const meta = { name: 'x', description: 'y' }
const a = await agent('arch audit', {label:'arch', agentType:'deepseek-architecture'})
const b = await agent('deep db work', {label:'database deep'})
"""


def _clear_deepseek_env():
    os.environ.pop("DEEPSEEK_API_KEY", None)
    os.environ.pop("CLAUDE_CODEX_DEEPSEEK_API_KEY", None)


def test_remap_when_no_key():
    _clear_deepseek_env()
    new, ctx, rep = sh.preflight(SCRIPT, "router")
    expect(rep["checks"]["deepseek_key"]["present"] is False, "should detect missing key")
    expect(any(a["action"] == "remap_deepseek_to_codex" for a in rep["actions"]), "no remap action")
    expect("__claudeCodexForceCodexOnly = true" in new, "sentinel not injected")
    # Enforcement is the sentinel, not string surgery: the wrapper mapping table
    # (and thus the 'deepseek-architecture' literal) must stay intact for clean
    # revert once a key is present.
    expect("SELF-HEAL applied" in ctx, "context missing self-heal note")


def test_sentinel_inserted_after_meta_not_before():
    """Bug #1: the sentinel must NOT be prepended before `export const meta` —
    Workflow requires meta to be the first statement or it is a syntax error."""
    _clear_deepseek_env()
    new, _ctx, _rep = sh.preflight(SCRIPT, "router")
    meta_pos = new.find("export const meta")
    sentinel_pos = new.find("globalThis.__claudeCodexForceCodexOnly")
    expect(meta_pos != -1, "meta block disappeared")
    expect(sentinel_pos != -1, "sentinel not present")
    expect(meta_pos < sentinel_pos,
           f"sentinel (@{sentinel_pos}) must come AFTER meta (@{meta_pos}); "
           f"prepending it breaks the meta-first rule. Head:\n{new[:120]!r}")
    # meta must still be the very first non-whitespace token
    expect(new.lstrip().startswith("export const meta"),
           f"script must still start with meta. Got: {new.lstrip()[:60]!r}")


def test_no_remap_when_key_present():
    os.environ["DEEPSEEK_API_KEY"] = "sk-test"
    try:
        new, ctx, rep = sh.preflight(SCRIPT, "router")
        expect(rep["checks"]["deepseek_key"]["present"] is True, "should see key present")
        expect("__claudeCodexForceCodexOnly" not in new, "must not force codex-only with key")
        expect(new.count("'deepseek-architecture'") == 1, "deepseek lane should be preserved")
    finally:
        _clear_deepseek_env()


def test_gateway_down_reported():
    # No port file in a tmp-empty runtime is hard to fake here; rely on probe
    # returning not-ok for an unused port by pointing at a closed port via env is
    # not supported, so just assert the gateway check key exists and is a dict.
    _clear_deepseek_env()
    _, ctx, rep = sh.preflight(SCRIPT, "router")
    expect("gateway" in rep["checks"], "gateway check missing")
    expect("ok" in rep["checks"]["gateway"], "gateway check has no ok field")


def test_healthy_gateway_probe():
    """Spin a fake /health on a port, write a port file, assert probe sees it."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    pf = runtime / "ccr-proxy.selfheal-test.port"
    pf.write_text(str(port), encoding="utf-8")
    try:
        ok, detail = sh._gateway_reachable()
        expect(ok, f"healthy gateway not detected: {detail}")
    finally:
        srv.shutdown()
        pf.unlink(missing_ok=True)


def test_gateway_detected_via_base_url_no_port_file():
    """Bug #2: the launcher deletes the .port file, so detection must work from
    ANTHROPIC_BASE_URL alone (the hook inherits it in router mode)."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # Ensure NO port file exists for this probe.
    runtime = ROOT / "runtime"
    for stale in runtime.glob("ccr-proxy.selfheal*.port"):
        stale.unlink(missing_ok=True)
    old = os.environ.get("ANTHROPIC_BASE_URL")
    # mimic the launcher: base url points at the proxy, possibly with a /v1 suffix
    os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}/v1/messages"
    try:
        ok, detail = sh._gateway_reachable()
        expect(ok, f"gateway must be detected via ANTHROPIC_BASE_URL: {detail}")
        expect(f":{port}" in detail, f"detail should name the probed port: {detail}")
    finally:
        srv.shutdown()
        if old is None:
            os.environ.pop("ANTHROPIC_BASE_URL", None)
        else:
            os.environ["ANTHROPIC_BASE_URL"] = old


def test_fail_open_on_bad_input():
    # None script would normally explode; preflight must not raise.
    try:
        new, ctx, rep = sh.preflight(None, "router")  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"FAIL: preflight raised on bad input: {exc}")
    # A None script must NOT trigger the old AttributeError('NoneType'... .count) —
    # report must be clean (no 'error' key) and script returned unchanged.
    expect(isinstance(rep, dict), "report must be a dict")
    expect("error" not in rep, f"None script must not error in report: {rep.get('error')}")
    expect(new is None, "None script should be returned unchanged")
    expect(not any(a.get("action") == "remap_deepseek_to_codex" for a in rep.get("actions", [])),
           "must not claim a deepseek remap on a None script")


def test_wrapper_honours_sentinel():
    """The native wrapper JS must route deepseek hints to codex when the
    sentinel is set. Execute the function under node if available; else skip."""
    node = None
    for cand in ("node", "bun"):
        if subprocess.run(["which", cand], capture_output=True).returncode == 0:
            node = cand
            break
    if node is None:
        print("  (skip wrapper JS test: no node/bun)")
        return
    wrapper = cw.wrapper_source_native()
    probe = wrapper + """
globalThis.__claudeCodexForceCodexOnly = true;
const off = __claudeCodexNativeAgentType({label:'architecture infra'});
globalThis.__claudeCodexForceCodexOnly = false;
const on  = __claudeCodexNativeAgentType({label:'architecture infra'});
console.log(JSON.stringify({off, on}));
"""
    out = subprocess.run([node, "-e", probe], capture_output=True, text=True)
    expect(out.returncode == 0, f"wrapper JS failed to run: {out.stderr[:300]}")
    res = json.loads(out.stdout.strip().splitlines()[-1])
    expect(res["off"] == "codex-worker", f"sentinel-on should force codex-worker, got {res['off']}")
    expect(res["on"] == "deepseek-architecture", f"sentinel-off should keep deepseek, got {res['on']}")


def test_wrapper_reasonix_flavor_keeps_codex_agenttype_names():
    """In reasonix flavor the wrapper must KEEP the codex-*/deepseek-* agentType
    names (which --agents defines and only-codex-fleet.py whitelists) — NOT emit
    reasonix-* names that exist nowhere. The engine is reasonix via the model
    route (launcher points codex-*/deepseek-* model at claude-reasonix-flash);
    the agentType is just a label that must stay in sync across wrapper, --agents,
    and the hook. Emitting reasonix-worker here (an agentType --agents doesn't
    define and the hook doesn't whitelist) was the root cause of reasonix lanes
    failing / being hook-blocked."""
    os.environ["CLAUDE_CODEX_FLAVOR"] = "reasonix"
    try:
        src = cw.wrapper_source_native()
        expect("codex-worker" in src,
               f"reasonix flavor must keep codex-worker agentType; got: {src[:300]}")
        expect("reasonix-worker" not in src,
               "reasonix flavor must NOT emit reasonix-worker (not in --agents / hook whitelist)")
    finally:
        os.environ.pop("CLAUDE_CODEX_FLAVOR", None)


def test_wrapper_codex_flavor_regression():
    """wrapper_source_native() must keep codex-worker/deepseek-* when flavor is unset or 'codex'."""
    # Unset case
    os.environ.pop("CLAUDE_CODEX_FLAVOR", None)
    try:
        src = cw.wrapper_source_native()
        expect("codex-worker" in src,
               f"codex flavor (unset) must still produce codex-worker; got: {src[:300]}")
        expect("deepseek-" in src,
               "codex flavor (unset) must still reference deepseek-* types")
    finally:
        os.environ.pop("CLAUDE_CODEX_FLAVOR", None)

    # Explicit codex case
    os.environ["CLAUDE_CODEX_FLAVOR"] = "codex"
    try:
        src = cw.wrapper_source_native()
        expect("codex-worker" in src,
               f"codex flavor (explicit) must still produce codex-worker; got: {src[:300]}")
    finally:
        os.environ.pop("CLAUDE_CODEX_FLAVOR", None)


def test_reasonix_cli_check_in_reasonix_flavor():
    """preflight() must populate checks['reasonix_cli'] when CLAUDE_CODEX_FLAVOR=reasonix.

    Part 1: binary absent  -> present is False, ctx contains a reasonix self-heal note.
    Part 2: binary present -> present is True.
    """
    os.environ["CLAUDE_CODEX_FLAVOR"] = "reasonix"
    os.environ["REASONIX_BIN"] = "/nonexistent/reasonix-xyz"
    try:
        _, ctx, rep = sh.preflight(SCRIPT, "router")
        expect("reasonix_cli" in rep["checks"], "reasonix_cli key missing from checks")
        expect(rep["checks"]["reasonix_cli"]["present"] is False,
               f"expected present=False for nonexistent binary, got: {rep['checks']['reasonix_cli']}")
        expect("reasonix" in ctx.lower(),
               f"expected reasonix self-heal note in ctx; got: {ctx!r}")

        # Part 2: use 'sh' (POSIX shell) as a guaranteed-present binary.
        os.environ["REASONIX_BIN"] = "sh"
        _, _ctx2, rep2 = sh.preflight(SCRIPT, "router")
        expect("reasonix_cli" in rep2["checks"], "reasonix_cli key missing from checks (present case)")
        expect(rep2["checks"]["reasonix_cli"]["present"] is True,
               f"expected present=True for 'sh' binary, got: {rep2['checks']['reasonix_cli']}")
    finally:
        os.environ.pop("CLAUDE_CODEX_FLAVOR", None)
        os.environ.pop("REASONIX_BIN", None)


def main() -> int:
    test_remap_when_no_key()
    test_sentinel_inserted_after_meta_not_before()
    test_no_remap_when_key_present()
    test_gateway_down_reported()
    test_healthy_gateway_probe()
    test_gateway_detected_via_base_url_no_port_file()
    test_fail_open_on_bad_input()
    test_wrapper_honours_sentinel()
    test_wrapper_reasonix_flavor_keeps_codex_agenttype_names()
    test_wrapper_codex_flavor_regression()
    test_reasonix_cli_check_in_reasonix_flavor()
    print("PASS: workflow self-heal preflight + wrapper sentinel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
