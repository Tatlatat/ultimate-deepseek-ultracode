#!/usr/bin/env python3
"""Regression: /v1/messages with a codex_cli model must emit streaming heartbeats
even when the client did NOT set stream=true.

Root cause being fixed: the workflow watchdog kills an agent() lane at exactly
180s when it sees no "visible content progress". The gateway only emitted the
keepalive heartbeat on the `payload.get("stream")` branch; a non-stream request
fell through to a blocking `send_json(200, blob)` that produced zero progress
events, so any codex lane running >180s was interrupted. ~34% of real workflow
lanes (those sent without stream=true) died this way at exactly 180.0s.

This test drives the gateway's do_POST /v1/messages handler against a fake
codex_cli producer that blocks, and asserts that a non-stream request now goes
down the heartbeat path (emits message_start + content_block_delta) rather than
blocking silently into a single JSON blob.

No network, no real codex: call_openai_compatible is monkeypatched. The handler
is driven with fake rfile/wfile so nothing touches a live gateway.
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
GW_PATH = ROOT / "codex-native-gateway.py"

spec = importlib.util.spec_from_file_location("codex_native_gateway_hb", GW_PATH)
gw = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


class FakeWFile(io.BytesIO):
    def flush(self):
        pass


class FakeHandler(gw.Handler):
    """Drive do_POST without a socket."""

    def __init__(self, body: bytes):
        self._body = body
        self.path = "/v1/messages"
        self.command = "POST"
        self.headers = {"content-length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.wfile = FakeWFile()
        self.status = None
        self.sent_headers = {}

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, k, v):
        self.sent_headers[k.lower()] = v

    def end_headers(self):
        pass

    @property
    def out(self) -> str:
        return self.wfile.getvalue().decode("utf-8", "replace")


def install_fake_codex(registry_model="claude-codex-pro", block_secs=0.0):
    """Force a codex_cli registry entry and a producer that optionally blocks."""
    orig_registry = gw.model_registry
    orig_call = gw.call_openai_compatible

    def fake_registry():
        return {registry_model: {"provider": "codex_cli"}}

    def fake_call(payload, model, config):
        if block_secs:
            time.sleep(block_secs)
        return {
            "id": "msg_test", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": "PONG"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    gw.model_registry = fake_registry
    gw.call_openai_compatible = fake_call
    return lambda: (setattr(gw, "model_registry", orig_registry),
                    setattr(gw, "call_openai_compatible", orig_call))


def test_nonstream_codex_emits_heartbeat():
    """A non-stream codex request must produce SSE progress (message_start +
    content_block_delta heartbeat), NOT a single silent JSON blob."""
    restore = install_fake_codex()
    try:
        body = json.dumps({
            "model": "claude-codex-pro", "max_tokens": 16,
            "messages": [{"role": "user", "content": "say PONG"}],
            # NOTE: no "stream": true  -> this is the path that used to block.
        }).encode()
        h = FakeHandler(body)
        h.do_POST()
        out = h.out
        expect("event: message_start" in out,
               f"non-stream codex must emit message_start (heartbeat path). Got:\n{out[:400]}")
        expect("content_block_delta" in out or "content_block_start" in out,
               f"non-stream codex must open a content block for the watchdog. Got:\n{out[:400]}")
        expect("PONG" in out, f"final content must still arrive. Got:\n{out[:400]}")
        expect(h.sent_headers.get("content-type", "").startswith("text/event-stream"),
               f"non-stream codex should now stream SSE. content-type={h.sent_headers.get('content-type')}")
    finally:
        restore()


def test_stream_true_still_works():
    """The existing stream=true path must be unchanged."""
    restore = install_fake_codex()
    try:
        body = json.dumps({
            "model": "claude-codex-pro", "max_tokens": 16, "stream": True,
            "messages": [{"role": "user", "content": "say PONG"}],
        }).encode()
        h = FakeHandler(body)
        h.do_POST()
        out = h.out
        expect("event: message_start" in out, "stream=true must still emit message_start")
        expect("PONG" in out, "stream=true must still deliver content")
    finally:
        restore()


def test_heartbeat_fires_before_slow_producer_returns():
    """With a producer that blocks longer than the keepalive interval, the
    heartbeat delta must reach the wire BEFORE the producer finishes — that is
    exactly what keeps the 180s watchdog from firing."""
    import os
    os.environ["CLAUDE_CODEX_GATEWAY_STREAM_KEEPALIVE_SECONDS"] = "1"
    restore = install_fake_codex(block_secs=2.5)
    try:
        body = json.dumps({
            "model": "claude-codex-pro", "max_tokens": 16,
            "messages": [{"role": "user", "content": "slow"}],
        }).encode()
        h = FakeHandler(body)
        h.do_POST()
        out = h.out
        # At least one heartbeat delta (single space) should appear given a
        # 2.5s producer block and 1s keepalive interval.
        expect(out.count("content_block_delta") >= 1,
               f"expected >=1 heartbeat delta during a slow producer. Got count={out.count('content_block_delta')}")
    finally:
        restore()
        os.environ.pop("CLAUDE_CODEX_GATEWAY_STREAM_KEEPALIVE_SECONDS", None)


def install_fake_reasonix(registry_model="claude-reasonix-flash"):
    """Force a reasonix_cli registry entry and stub out run_reasonix_acp."""
    orig_registry = gw.model_registry
    orig_run_reasonix_acp = gw.run_reasonix_acp

    def fake_registry():
        return {registry_model: {"provider": "reasonix_cli"}}

    def fake_run_reasonix_acp(prompt, config):
        return ("PONG", {
            "input_tokens": 1,
            "output_tokens": 1,
            "reasonix_cost_usd": None,
            "reasonix_cache_pct": None,
        })

    gw.model_registry = fake_registry
    gw.run_reasonix_acp = fake_run_reasonix_acp
    return lambda: (setattr(gw, "model_registry", orig_registry),
                    setattr(gw, "run_reasonix_acp", orig_run_reasonix_acp))


def test_nonstream_reasonix_emits_heartbeat():
    """A non-stream reasonix request must produce SSE heartbeats, NOT a JSON blob."""
    restore = install_fake_reasonix()
    try:
        body = json.dumps({
            "model": "claude-reasonix-flash", "max_tokens": 16,
            "messages": [{"role": "user", "content": "say PONG"}],
            # NOTE: no "stream": true  -> this is the path that must now go through heartbeat.
        }).encode()
        h = FakeHandler(body)
        h.do_POST()
        out = h.out
        expect("event: message_start" in out,
               f"non-stream reasonix must emit message_start (heartbeat path). Got:\n{out[:400]}")
        expect("PONG" in out, f"final content must still arrive. Got:\n{out[:400]}")
    finally:
        restore()


def main() -> int:
    test_nonstream_codex_emits_heartbeat()
    test_stream_true_still_works()
    test_heartbeat_fires_before_slow_producer_returns()
    test_nonstream_reasonix_emits_heartbeat()
    print("PASS: gateway non-stream codex heartbeat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
