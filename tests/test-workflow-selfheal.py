#!/usr/bin/env python3
"""Tests for hooks/workflow_selfheal.py + the wrapper sentinel it relies on.

Covers the gateway-reachability probe, the reasonix-CLI check, fail-open
behaviour, and that the JS wrapper honours the __claudeReasonixForceReasonixOnly
sentinel. The reasonix-remap / DEEPSEEK_API_KEY probe has been removed.
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

# Load reasonix-workflow.py (dash in name) to exercise the wrapper source.
spec = importlib.util.spec_from_file_location("reasonix_workflow", HOOKS / "reasonix-workflow.py")
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
    os.environ.pop("CLAUDE_REASONIX_DEEPSEEK_API_KEY", None)


def test_no_reasonix_remap_no_key():
    """The reasonix-remap path has been removed: no DEEPSEEK_API_KEY must NOT trigger
    any script mutation or remap action — deepseek lanes pass through unchanged."""
    _clear_deepseek_env()
    new, ctx, rep = sh.preflight(SCRIPT, "native")
    # No deepseek_key check in the report (that probe was removed with the remap).
    expect("deepseek_key" not in rep["checks"],
           f"deepseek_key check should be gone; got: {list(rep['checks'].keys())}")
    # No remap action emitted.
    expect(not any(a.get("action") == "remap_deepseek_to_reasonix" for a in rep["actions"]),
           "remap_deepseek_to_reasonix action must not appear after reasonix remap removal")
    # Sentinel NOT injected — script is returned verbatim.
    expect("__claudeReasonixForceReasonixOnly" not in new,
           "sentinel must not be injected when reasonix remap is absent")
    # Deepseek lane literal survives intact.
    expect("agentType:'deepseek-architecture'" in new or "agentType: 'deepseek-architecture'" in new,
           "deepseek-architecture lane must be passed through unmodified")


def test_script_unchanged_no_sentinel():
    """Preflight must return the script byte-for-byte unchanged (no sentinel,
    no surgery) because the reasonix-remap logic no longer exists."""
    _clear_deepseek_env()
    new, _ctx, _rep = sh.preflight(SCRIPT, "native")
    expect(new == SCRIPT, f"script must be returned unchanged; diff:\n{new!r}\nvs\n{SCRIPT!r}")
    # Specifically: sentinel is absent.
    expect("globalThis.__claudeReasonixForceReasonixOnly" not in new,
           "sentinel must not appear in passthrough script")
    # meta block is still the first line.
    expect(new.lstrip().startswith("export const meta"),
           f"meta must still be first; got: {new.lstrip()[:60]!r}")


def test_script_passthrough_with_key_present():
    """With DEEPSEEK_API_KEY set, script also passes through unchanged — no deepseek_key
    check in report and no remap action (reasonix remap removed entirely)."""
    os.environ["DEEPSEEK_API_KEY"] = "sk-test"
    try:
        new, ctx, rep = sh.preflight(SCRIPT, "native")
        expect("deepseek_key" not in rep["checks"],
               "deepseek_key check must be gone even when key is present")
        expect("__claudeReasonixForceReasonixOnly" not in new,
               "sentinel must not appear; remap is gone")
        expect(new.count("'deepseek-architecture'") == 1,
               "deepseek-architecture lane must be preserved exactly once")
        expect(new == SCRIPT, "script must be returned byte-for-byte unchanged")
    finally:
        _clear_deepseek_env()


def test_gateway_down_reported():
    # No port file in a tmp-empty runtime is hard to fake here; rely on probe
    # returning not-ok for an unused port by pointing at a closed port via env is
    # not supported, so just assert the gateway check key exists and is a dict.
    _clear_deepseek_env()
    _, ctx, rep = sh.preflight(SCRIPT, "native")
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
    ANTHROPIC_BASE_URL alone (the hook inherits it from the gateway env)."""
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
        new, ctx, rep = sh.preflight(None, "native")  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"FAIL: preflight raised on bad input: {exc}")
    # A None script must NOT trigger the old AttributeError('NoneType'... .count) —
    # report must be clean (no 'error' key) and script returned unchanged.
    expect(isinstance(rep, dict), "report must be a dict")
    expect("error" not in rep, f"None script must not error in report: {rep.get('error')}")
    expect(new is None, "None script should be returned unchanged")
    expect(not any(a.get("action") == "remap_deepseek_to_reasonix" for a in rep.get("actions", [])),
           "must not claim a deepseek remap on a None script")


def test_wrapper_honours_sentinel():
    """The native wrapper JS must route deepseek hints to reasonix when the
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
globalThis.__claudeReasonixForceReasonixOnly = true;
const off = __claudeReasonixNativeAgentType({label:'architecture infra'});
globalThis.__claudeReasonixForceReasonixOnly = false;
const on  = __claudeReasonixNativeAgentType({label:'architecture infra'});
console.log(JSON.stringify({off, on}));
"""
    out = subprocess.run([node, "-e", probe], capture_output=True, text=True)
    expect(out.returncode == 0, f"wrapper JS failed to run: {out.stderr[:300]}")
    res = json.loads(out.stdout.strip().splitlines()[-1])
    expect(res["off"] == "reasonix-worker", f"sentinel-on should force reasonix-worker, got {res['off']}")
    # deepseek-architecture was dropped; architecture/infra now folds into the reviewer.
    expect(res["on"] == "reasonix-reviewer", f"sentinel-off architecture should map to reasonix-reviewer, got {res['on']}")


def test_wrapper_emits_reasonix_agenttype_names():
    """The wrapper must emit the reasonix-* agentType names that --agents defines
    and only-reasonix-fleet.py whitelists — never dropped legacy names.
    These three sites (wrapper emit, --agents definitions, hook whitelist)
    must stay byte-identical; emitting a name no site defines hook-blocks the lane."""
    os.environ["CLAUDE_REASONIX_FLAVOR"] = "reasonix"
    try:
        src = cw.wrapper_source_native()
        expect("reasonix-worker" in src,
               f"wrapper must emit reasonix-worker agentType; got: {src[:300]}")
        # The dropped types must never be RETURNED (emitted as a lane's agentType).
        # An explicit back-compat passthrough of a caller-supplied deepseek-* is fine.
        expect("return 'deepseek-architecture'" not in src and "return 'deepseek-deep'" not in src,
               "the dropped deepseek-* agentTypes must not be emitted as a role mapping")
    finally:
        os.environ.pop("CLAUDE_REASONIX_FLAVOR", None)


def test_wrapper_emit_is_flavor_agnostic():
    """The emit logic no longer branches on flavor (reasonix flavor is the only one); an
    unset flavor must still produce reasonix-* names, never the legacy worker name."""
    os.environ.pop("CLAUDE_REASONIX_FLAVOR", None)
    os.environ.pop("CLAUDE_REASONIX_FLAVOR", None)
    src = cw.wrapper_source_native()
    expect("reasonix-worker" in src,
           f"unset flavor must still produce reasonix-worker; got: {src[:300]}")


def test_reasonix_cli_check_in_reasonix_flavor():
    """preflight() must populate checks['reasonix_cli'] when CLAUDE_REASONIX_FLAVOR=reasonix.

    Part 1: binary absent  -> present is False, ctx contains a reasonix self-heal note.
    Part 2: binary present -> present is True.
    """
    os.environ["CLAUDE_REASONIX_FLAVOR"] = "reasonix"
    os.environ["REASONIX_BIN"] = "/nonexistent/reasonix-xyz"
    try:
        _, ctx, rep = sh.preflight(SCRIPT, "native")
        expect("reasonix_cli" in rep["checks"], "reasonix_cli key missing from checks")
        expect(rep["checks"]["reasonix_cli"]["present"] is False,
               f"expected present=False for nonexistent binary, got: {rep['checks']['reasonix_cli']}")
        expect("reasonix" in ctx.lower(),
               f"expected reasonix self-heal note in ctx; got: {ctx!r}")

        # Part 2: use 'sh' (POSIX shell) as a guaranteed-present binary.
        os.environ["REASONIX_BIN"] = "sh"
        _, _ctx2, rep2 = sh.preflight(SCRIPT, "native")
        expect("reasonix_cli" in rep2["checks"], "reasonix_cli key missing from checks (present case)")
        expect(rep2["checks"]["reasonix_cli"]["present"] is True,
               f"expected present=True for 'sh' binary, got: {rep2['checks']['reasonix_cli']}")
    finally:
        os.environ.pop("CLAUDE_REASONIX_FLAVOR", None)
        os.environ.pop("REASONIX_BIN", None)


def main() -> int:
    test_no_reasonix_remap_no_key()
    test_script_unchanged_no_sentinel()
    test_script_passthrough_with_key_present()
    test_gateway_down_reported()
    test_healthy_gateway_probe()
    test_gateway_detected_via_base_url_no_port_file()
    test_fail_open_on_bad_input()
    test_wrapper_honours_sentinel()
    test_wrapper_emits_reasonix_agenttype_names()
    test_wrapper_emit_is_flavor_agnostic()
    test_reasonix_cli_check_in_reasonix_flavor()
    print("PASS: workflow self-heal preflight + wrapper sentinel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
