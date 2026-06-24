"""E2E test of the FORCED StructuredOutput path — the exact path a Dynamic Workflow
agent({schema}) lane takes, and where real workflows fail ("completed without
calling StructuredOutput", empty lane, schema mismatch).

It drives the REAL gateway over HTTP with reasonix SIMULATED (env-injected reply,
no DeepSeek spend), so the request flows through openai_messages_to_prompt ->
run_reasonix_acp -> parse-text->tool_use -> forced-fallback — the whole chain the
old text-only mock skipped. Asserts:

  1. reasonix returns clean JSON  -> gateway emits a StructuredOutput tool_use;
  2. reasonix NARRATES (no JSON) but the tool is FORCED -> gateway synthesizes a
     schema-valid tool_use anyway (lane never comes back empty);
  3. reasonix wraps JSON in prose+fences -> gateway still extracts the object.
"""
from __future__ import annotations
import importlib.util, json, os, threading, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


SCHEMA = {  # a nested schema like the real review/research lanes use
    "type": "object", "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": {
        "type": "object", "properties": {"title": {"type": "string"}},
        "required": ["title"]}}},
}


def _start():
    # NOT mock-mode (that short-circuits before the reasonix path). Instead simulate
    # reasonix's text reply so the full structured path runs.
    os.environ.pop("CLAUDE_REASONIX_GATEWAY_MOCK", None)
    httpd = gw.ThreadingHTTPServer(("127.0.0.1", 0), gw.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _post_lane(port: int) -> dict:
    body = json.dumps({
        "model": "claude-reasonix-flash", "max_tokens": 256, "stream": False,
        "messages": [{"role": "user", "content": "Review and report findings."}],
        "tools": [{"name": "StructuredOutput", "input_schema": SCHEMA}],
        "tool_choice": {"type": "tool", "name": "StructuredOutput"},  # FORCED
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": "local",
                 "anthropic-version": "2023-06-01"})
    r = urllib.request.urlopen(req, timeout=30)
    raw = r.read().decode("utf-8", "ignore")
    if raw.lstrip().startswith("{"):
        return json.loads(raw)
    # Reassemble the Anthropic SSE stream the way the Claude Code client does:
    # content_block_start opens a block; tool_use input arrives as input_json_delta
    # partial_json chunks that must be concatenated and parsed.
    blocks: dict[int, dict] = {}
    partial: dict[int, str] = {}
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except Exception:
            continue
        t = ev.get("type")
        if t == "content_block_start":
            blocks[ev["index"]] = dict(ev.get("content_block", {}))
        elif t == "content_block_delta":
            d = ev.get("delta", {})
            if d.get("type") == "input_json_delta":
                partial[ev["index"]] = partial.get(ev["index"], "") + d.get("partial_json", "")
            elif d.get("type") == "text_delta":
                b = blocks.get(ev["index"], {})
                b["text"] = b.get("text", "") + d.get("text", "")
    content = []
    for idx, b in sorted(blocks.items()):
        if b.get("type") == "tool_use" and idx in partial:
            try:
                b["input"] = json.loads(partial[idx])
            except Exception:
                b["input"] = {}
        content.append(b)
    return {"content": content}


def _has_tool_use(resp: dict) -> dict | None:
    for b in resp.get("content", []):
        if isinstance(b, dict) and b.get("type") == "tool_use":
            return b
    return None


def test_clean_json_becomes_tool_use():
    os.environ["CLAUDE_REASONIX_GATEWAY_MOCK_REASONIX_TEXT"] = json.dumps(
        {"findings": [{"title": "real finding"}]})
    httpd, port = _start()
    try:
        resp = _post_lane(port)
        tu = _has_tool_use(resp)
        expect(tu is not None, "forced StructuredOutput + JSON reply -> a tool_use block")
        expect(tu["name"] == "StructuredOutput", "tool_use names StructuredOutput")
        expect(isinstance(tu["input"].get("findings"), list), "tool_use input matches schema")
    finally:
        httpd.shutdown()
        os.environ.pop("CLAUDE_REASONIX_GATEWAY_MOCK_REASONIX_TEXT", None)


def test_narration_still_yields_forced_tool_use():
    # The real killer: DeepSeek narrates instead of emitting JSON. Because the tool
    # is FORCED, the lane must STILL come back with a schema-valid tool_use, never empty.
    os.environ["CLAUDE_REASONIX_GATEWAY_MOCK_REASONIX_TEXT"] = \
        "I have completed the review and returned the findings via StructuredOutput."
    httpd, port = _start()
    try:
        resp = _post_lane(port)
        tu = _has_tool_use(resp)
        expect(tu is not None, "forced tool + narration -> synthesized schema-valid tool_use (not empty)")
        expect("findings" in tu["input"], "synthesized fallback has the required schema key")
    finally:
        httpd.shutdown()
        os.environ.pop("CLAUDE_REASONIX_GATEWAY_MOCK_REASONIX_TEXT", None)


def test_fenced_json_is_extracted():
    os.environ["CLAUDE_REASONIX_GATEWAY_MOCK_REASONIX_TEXT"] = (
        "Here are the results:\n```json\n" + json.dumps({"findings": [{"title": "x"}]}) + "\n```\nDone.")
    httpd, port = _start()
    try:
        resp = _post_lane(port)
        tu = _has_tool_use(resp)
        expect(tu is not None, "fenced+prefixed JSON -> tool_use")
        expect(tu["input"]["findings"][0]["title"] == "x", "the fenced object is extracted intact")
    finally:
        httpd.shutdown()
        os.environ.pop("CLAUDE_REASONIX_GATEWAY_MOCK_REASONIX_TEXT", None)


if __name__ == "__main__":
    test_clean_json_becomes_tool_use()
    test_narration_still_yields_forced_tool_use()
    test_fenced_json_is_extracted()
    print("PASS: e2e structured output")
