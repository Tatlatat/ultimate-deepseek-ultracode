"""E2E test for the 'hollow lane' bug found by the multi-agent audit: when reasonix
returns EMPTY text (timeout-ish / produced nothing), the gateway's lazy SSE path
finalized a syntactically valid stream with ZERO real content blocks and NO error.
The workflow lane then comes back silently empty — no answer, no failure surfaced.

Drives the REAL gateway over HTTP with reasonix simulated to return "" and asserts
the stream carries SOMETHING actionable: either a non-empty content block or an
explicit error event — never a clean-but-hollow message_stop.
"""
from __future__ import annotations
import importlib.util, json, os, threading, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "codex-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def _start():
    os.environ.pop("CLAUDE_CODEX_GATEWAY_MOCK", None)
    httpd = gw.ThreadingHTTPServer(("127.0.0.1", 0), gw.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _stream(port: int, body: dict) -> list:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages", data=data,
        headers={"content-type": "application/json", "x-api-key": "local",
                 "anthropic-version": "2023-06-01"})
    raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    events = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except Exception:
                pass
    return events


def _has_real_text(events) -> bool:
    for ev in events:
        if ev.get("type") == "content_block_delta":
            d = ev.get("delta", {})
            if d.get("type") == "text_delta" and d.get("text", "").strip():
                return True
        if ev.get("type") == "content_block_start":
            cb = ev.get("content_block", {})
            if cb.get("type") == "tool_use":
                return True
    return False


def _has_error(events) -> bool:
    return any(ev.get("type") == "error" for ev in events)


def test_empty_reasonix_reply_is_not_silently_hollow():
    # reasonix returns nothing. A plain (no-tool) lane must NOT come back as a clean
    # empty stream — it must carry a real text block or an explicit error.
    os.environ["CLAUDE_CODEX_GATEWAY_MOCK_REASONIX_TEXT"] = ""
    httpd, port = _start()
    try:
        events = _stream(port, {
            "model": "claude-reasonix-flash", "max_tokens": 64,
            "messages": [{"role": "user", "content": "do the task"}],
        })
        # The bug: stream ends clean with no content and no error. After the fix the
        # stream must carry real text OR an explicit error event.
        expect(_has_real_text(events) or _has_error(events),
               "empty reasonix reply must surface real content or an error, not a hollow stream")
    finally:
        httpd.shutdown()
        os.environ.pop("CLAUDE_CODEX_GATEWAY_MOCK_REASONIX_TEXT", None)


def test_nonempty_reply_still_streams_its_text():
    # Guard against over-correction: a real answer must still stream through unchanged.
    os.environ["CLAUDE_CODEX_GATEWAY_MOCK_REASONIX_TEXT"] = "here is the real answer"
    httpd, port = _start()
    try:
        events = _stream(port, {
            "model": "claude-reasonix-flash", "max_tokens": 64,
            "messages": [{"role": "user", "content": "do the task"}],
        })
        expect(_has_real_text(events), "a real reasonix answer still streams its text")
        expect(not _has_error(events), "a real answer does not spuriously emit an error")
    finally:
        httpd.shutdown()
        os.environ.pop("CLAUDE_CODEX_GATEWAY_MOCK_REASONIX_TEXT", None)


if __name__ == "__main__":
    test_empty_reasonix_reply_is_not_silently_hollow()
    test_nonempty_reply_still_streams_its_text()
    print("PASS: e2e hollow lane")
