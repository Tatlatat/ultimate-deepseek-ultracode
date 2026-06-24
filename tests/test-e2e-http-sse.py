"""End-to-end HTTP/SSE test — exercises the REAL request path a workflow lane takes,
not a synthetic in-process call. Spins the actual gateway HTTP server in MOCK mode
(no DeepSeek spend), sends real /v1/messages requests over a socket, and asserts:

  1. a normal streaming request returns a well-formed SSE stream;
  2. a client that DISCONNECTS mid-stream does NOT crash the gateway (the 272
     BrokenPipeError tracebacks seen in production), and the server keeps serving
     subsequent requests.

This is the kind of test that was missing: unit tests called functions directly and
never touched the socket/SSE/disconnect path where the real failures lived.
"""
from __future__ import annotations
import importlib.util, json, os, socket, threading, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def _start_server():
    os.environ["CLAUDE_REASONIX_GATEWAY_MOCK"] = "1"
    httpd = gw.ThreadingHTTPServer(("127.0.0.1", 0), gw.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def _post(port, body, stream=True, read_all=True):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages", data=data,
        headers={"content-type": "application/json", "x-api-key": "local",
                 "anthropic-version": "2023-06-01"})
    r = urllib.request.urlopen(req, timeout=30)
    if read_all:
        return r.read().decode("utf-8", "ignore")
    return r


def test_normal_stream_is_wellformed():
    httpd, port = _start_server()
    try:
        out = _post(port, {"model": "claude-reasonix-flash", "max_tokens": 16,
                           "stream": True,
                           "messages": [{"role": "user", "content": "hi"}]})
        expect("event:" in out and "data:" in out, "response is an SSE stream")
        expect("message_start" in out, "stream has message_start")
        expect("message_stop" in out or "message_delta" in out,
               "stream terminates with message_delta/stop")
    finally:
        httpd.shutdown()


def test_client_disconnect_midstream_does_not_crash_server():
    # Open a raw socket, send the request, read only the FIRST bytes, then slam the
    # connection shut — the gateway's SSE writes will hit a broken pipe. The server
    # must swallow it (no crash) and KEEP SERVING. We prove "keep serving" by making
    # a normal request afterward and requiring a clean response.
    httpd, port = _start_server()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        body = json.dumps({"model": "claude-reasonix-flash", "max_tokens": 64,
                           "stream": True,
                           "messages": [{"role": "user", "content": "stream then I leave"}]}).encode()
        req = (f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
               f"Connection: close\r\n\r\n").encode() + body
        s.sendall(req)
        # read a little, then abort hard (RST) to force BrokenPipe on the server side
        s.recv(64)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _linger_now())
        s.close()
        time.sleep(0.5)  # let the server-side write hit the dead socket

        # The server must still be alive and serving.
        out = _post(port, {"model": "claude-reasonix-flash", "max_tokens": 16,
                           "stream": True,
                           "messages": [{"role": "user", "content": "still alive?"}]})
        expect("event:" in out and "message_start" in out,
               "gateway keeps serving after a mid-stream client disconnect")
    finally:
        httpd.shutdown()


def _linger_now():
    import struct
    return struct.pack("ii", 1, 0)  # enable linger, timeout 0 -> RST on close


if __name__ == "__main__":
    test_normal_stream_is_wellformed()
    test_client_disconnect_midstream_does_not_crash_server()
    print("PASS: e2e http/sse")
