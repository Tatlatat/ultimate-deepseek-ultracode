#!/usr/bin/env python3
"""Small Anthropic Messages-compatible gateway for claude-reasonix native agents.

The gateway is intentionally local and session-scoped.  The claude-reasonix
launcher starts it, points only that Claude Code process at it, and then stops
it when Claude exits.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import time as _time
import traceback
from typing import Any
import urllib.error
import urllib.request
from uuid import uuid4

_GATEWAY_DIR = Path(__file__).resolve().parent
if str(_GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(_GATEWAY_DIR))
from reasonix_gateway.env import JSON, env_first, env_int, env_float, env_truthy
from reasonix_gateway.text import json_bytes, text_from_content, lane_task_text
from reasonix_gateway.cost import weighted_cache, classify_miss, append_reasonix_cost, summarize_reasonix_cost
from reasonix_gateway.harness import (_lane_fail_marker_on, lane_unverified_reply, _lane_harness_on,
    parse_harness_result, harness_lane_reply, lane_acceptance_test, _clean_acceptance_command)
from reasonix_gateway.levers import (
    _REASONIX_CLI_SEMAPHORE_LOCK, _REASONIX_CLI_SEMAPHORE,
    _PREINDEX_LOCK, _PREINDEX_DONE, _PREINDEX_NODE_SCRIPT,
    _PRIME_LOCK, _PRIME_GATES,
    _KEEPALIVE_LOCK, _KEEPALIVE_PREFIXES,
    _READ_SUMMARY_CACHE_LOCK, _READ_SUMMARY_CACHE, _READ_CACHE_LOADED,
    READ_CACHE_BLOCK_BEGIN, READ_CACHE_BLOCK_END, _FILE_PATH_RE,
    _PRIME_SERIAL_LOCK, _PRIME_SERIAL_COUNTS, _PRIME_SERIAL_LOCKS,
    _LANE_LOCK, _LANE_COUNTS,
    _SYNTHESIS_INTENT_RE, _READER_INTENT_RE, _EDIT_INTENT_RE, _READER_BROADEN_RE,
    _PREFETCH_PATH_RE, _OVERSCOPE_BULK_RE, _GUIDE_OPEN_MARKER, _GUIDE_CLOSE_MARKER,
    _NEGATION_RE, _BILLING_HEADER_RE,
    preindex_enabled, _preindex_node_bin, _preindex_engine_dist, build_preindex,
    gateway_trace, reasonix_cli_semaphore,
    _prime_dict_cap, _evict_oldest,
    _keepalive_enabled, record_keepalive_prefix, keepalive_targets,
    _read_cache_on, _read_cache_cap, _read_cache_ttl_s, _read_cache_max_bytes,
    _read_cache_path, _file_fingerprint, extract_file_paths_from_prompt,
    _read_cache_store, _read_cache_lookup, read_cache_injection_block,
    populate_read_cache, save_read_cache, load_read_cache,
    reset_prime_state, serial_lock_for, acquire_serial_slot,
    register_lane_attempt, should_force_fallback, clear_lane_count,
    prefix_prime_key, acquire_prime_role, model_registry,
    normalize_prefix,
    tool_schema_entries, schema_type, is_structured_output_tool_name,
    _schema_has_nested_array_of_objects,
    _reader_broaden_on, classify_lane_type, is_synthesis_prompt, is_heavy_synthesis,
    mapreduce_directive, context_budget_directive,
    _output_discipline_on, output_discipline_directive, output_discipline_budget,
    _read_summary_on, read_summary_budget, read_lane_summary_instruction,
    _overscope_on, _overscope_max_files, lane_file_scope_count,
    _strip_injected_guide, _bulk_scope_match, overscope_rejection,
)
from reasonix_gateway.engine_seam import (
    GatewayError,
    anthropic_system_to_text, anthropic_messages_to_openai,
    anthropic_tools_to_openai, anthropic_tool_choice_to_openai,
    openai_response_to_anthropic, estimate_tokens,
    provider_chat_payload, call_openai_compatible,
    fallback_value_from_schema, structured_timeout_fallback,
    structured_output_prompt_instruction, _tool_choice_forces,
    openai_messages_to_prompt, requested_structured_output_tool,
    tool_name_from_schema, tool_names_from_payload,
    structured_output_success_text, anthropic_has_successful_structured_output,
    parse_json_object_from_text, anthropic_tool_use_response,
    anthropic_end_turn_response, retry_cap_for_empty,
    run_reasonix_acp, call_openai_chat_completion,
)


class ClientGone(Exception):
    """The streaming client disconnected mid-response (BrokenPipe/ConnectionReset).
    Normal, not an error — the handler stops streaming and does NOT try to write an
    error body down the dead socket."""


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-reasonix-gateway/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.getenv("CLAUDE_REASONIX_GATEWAY_QUIET", os.getenv("CLAUDE_CODEX_GATEWAY_QUIET", "1")).lower() in {"1", "true", "yes", "on"}:
            return
        super().log_message(fmt, *args)

    def read_json(self) -> JSON:
        length = int(self.headers.get("content-length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:
            raise GatewayError(400, "invalid_request_error", f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise GatewayError(400, "invalid_request_error", "request body must be a JSON object")
        return data

    def send_json(self, status: int, data: Any, headers: dict[str, str] | None = None) -> None:
        body = json_bytes(data)
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, exc: GatewayError) -> None:
        self.send_json(exc.status, {"type": "error", "error": {"type": exc.error_type, "message": exc.message}})

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            self.send_json(200, {"ok": True, "time": time.time()})
            return
        if path == "/v1/models":
            models = [
                {
                    "id": model_id,
                    "type": "model",
                    "display_name": config["display_name"],
                    "created_at": 0,
                }
                for model_id, config in model_registry().items()
            ]
            self.send_json(200, {"data": models})
            return
        self.send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})

    def do_POST(self) -> None:
        try:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path in {"/v1/chat/completions", "/chat/completions"}:
                payload = self.read_json()
                model = str(payload.get("model") or "")
                registry = model_registry()
                if model not in registry:
                    raise GatewayError(400, "invalid_request_error", f"unknown model: {model}")
                config = registry[model]
                provider = config.get("provider")
                if payload.get("stream"):
                    # reasonix_cli runs a blocking subprocess that can exceed the 180s
                    # workflow watchdog; it needs the heartbeat-lazy SSE path so a lane
                    # is not killed mid-run with no visible progress.
                    if provider == "reasonix_cli":
                        self.send_openai_sse_response_lazy(
                            lambda: call_openai_chat_completion(payload, model, config)
                        )
                    else:
                        response = call_openai_chat_completion(payload, model, config)
                        self.send_openai_sse_response(response)
                else:
                    response = call_openai_chat_completion(payload, model, config)
                    self.send_json(200, response)
                return
            if path == "/v1/messages/count_tokens":
                payload = self.read_json()
                self.send_json(200, {"input_tokens": estimate_tokens(payload)})
                return
            if path == "/v1/messages":
                payload = self.read_json()
                model = str(payload.get("model") or "")
                registry = model_registry()
                if model in registry:
                    config = registry[model]
                    # reasonix_cli runs a blocking subprocess that can take >180s. The
                    # Claude Code workflow watchdog interrupts an agent() lane at
                    # exactly 180s if it sees no visible content progress. So ALWAYS
                    # take the heartbeat-streaming path for reasonix_cli, regardless of
                    # the client's stream flag: ~34% of real lanes were sent without
                    # stream=true and died silently at 180s on the old blocking blob
                    # path. The Claude Code client parses the SSE stream fine even
                    # when it did not request stream=true.
                    provider = config.get("provider")
                    if provider == "reasonix_cli":
                        self.send_sse_response_lazy(
                            lambda: call_openai_compatible(payload, model, config),
                            model,
                        )
                    elif payload.get("stream"):
                        response = call_openai_compatible(payload, model, config)
                        self.send_sse_response(response)
                    else:
                        response = call_openai_compatible(payload, model, config)
                        self.send_json(200, response)
                    return
                self.forward_anthropic(payload)
                return
            self.send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})
        except (ClientGone, BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Client hung up mid-response. Nothing to send (the socket is dead) and
            # nothing to log — this is normal streaming churn, not a gateway fault.
            return
        except GatewayError as exc:
            self._safe_send_error(exc)
        except Exception as exc:
            if os.getenv("CLAUDE_REASONIX_GATEWAY_DEBUG", os.getenv("CLAUDE_CODEX_GATEWAY_DEBUG", "")).lower() in {"1", "true", "yes", "on"}:
                traceback.print_exc(file=sys.stderr)
            self._safe_send_error(GatewayError(500, "api_error", str(exc)))

    def _safe_send_error(self, exc: "GatewayError") -> None:
        # Sending the error body can itself hit a dead socket (the client that caused
        # the error may already be gone). Never let that raise a second, noisy
        # traceback — the original error is what matters.
        try:
            self.send_error_json(exc)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ValueError):
            return

    def send_sse_event(self, event: str, data: Any) -> None:
        # A streaming client (CCR / the Claude Code workflow runtime) routinely
        # disconnects mid-stream — on timeout, cancel, or when a lane is superseded.
        # The socket write then raises BrokenPipe/ConnectionReset. That is NORMAL,
        # not a gateway error: swallow it and signal the caller to stop streaming so
        # we don't spew 272 tracebacks (measured in prod) or try to send an error
        # body down a dead socket. ClientGone is caught by the streaming loop.
        try:
            self.wfile.write(f"event: {event}\n".encode("utf-8"))
            self.wfile.write(b"data: ")
            self.wfile.write(json_bytes(data))
            self.wfile.write(b"\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            raise ClientGone() from exc

    def wait_for_stream_response(self, producer: Any, on_keepalive: Any = None) -> Any:
        result_queue: queue.Queue = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                result_queue.put(("response", producer()))
            except Exception as exc:
                result_queue.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()
        interval = max(1.0, float(os.getenv("CLAUDE_REASONIX_GATEWAY_STREAM_KEEPALIVE_SECONDS", os.getenv("CLAUDE_CODEX_GATEWAY_STREAM_KEEPALIVE_SECONDS", "10"))))
        while True:
            try:
                kind, value = result_queue.get(timeout=interval)
            except queue.Empty:
                # An idle tick. For the Anthropic lazy path we emit a real
                # content_block_delta heartbeat (via on_keepalive) so the Claude
                # Code workflow watchdog sees visible content progress and does not
                # fire its no-progress interrupt while reasonix exec is still buffering.
                # A bare ": keepalive" SSE comment keeps the socket warm but is
                # invisible to that watchdog, so it is only the fallback.
                try:
                    if on_keepalive is not None:
                        on_keepalive()
                    else:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
                    raise ClientGone() from exc
                continue
            if kind == "error":
                raise value
            return value

    def send_sse_response(self, message: JSON) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        self.write_sse_response_body(message)

    def send_sse_response_lazy(self, producer: Any, model: str = "") -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        # Preamble: emit message_start + open a heartbeat text block at index 0
        # BEFORE the producer is awaited. Anthropic streaming requires message_start
        # to precede any content_block event, so the synthetic envelope must be sent
        # first; the real blocks are then emitted shifted to indices >= 1.
        start_message = {
            "id": f"msg_{uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        self.send_sse_event("message_start", {"type": "message_start", "message": start_message})
        self.send_sse_event(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        )

        def on_keepalive() -> None:
            # A real content_block_delta (single space) resets the workflow watchdog.
            self.send_sse_event(
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " "}},
            )

        try:
            message = self.wait_for_stream_response(producer, on_keepalive=on_keepalive)
        except Exception as exc:
            # message_start is already on the wire: close the heartbeat block, then error.
            self.send_sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})
            self.send_sse_event("error", {"type": "error", "error": {"type": "api_error", "message": str(exc)}})
            return
        # Finalize: close heartbeat block, then emit the real blocks at indices >= 1
        # (do NOT re-emit message_start) followed by message_delta/message_stop.
        self.send_sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})
        self.write_sse_response_body(message, start_index=1, emit_message_start=False)

    def write_sse_response_body(self, message: JSON, start_index: int = 0, emit_message_start: bool = True) -> None:
        if emit_message_start:
            start_message = dict(message)
            start_message["content"] = []
            self.send_sse_event("message_start", {"type": "message_start", "message": start_message})
        next_index = start_index
        emitted_real = 0
        for index, block in enumerate(message.get("content") or [], start=start_index):
            next_index = index + 1
            block_type = block.get("type")
            if block_type == "text":
                if block.get("text", "").strip():
                    emitted_real += 1
                self.send_sse_event(
                    "content_block_start",
                    {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
                )
                self.send_sse_event(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": block.get("text", "")}},
                )
                self.send_sse_event("content_block_stop", {"type": "content_block_stop", "index": index})
            elif block_type == "tool_use":
                emitted_real += 1
                self.send_sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": {}},
                    },
                )
                self.send_sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": json.dumps(block.get("input") or {}, ensure_ascii=False)},
                    },
                )
                self.send_sse_event("content_block_stop", {"type": "content_block_stop", "index": index})

        # HOLLOW-LANE GUARD (found by the multi-agent audit): if the producer returned
        # no real content (empty/whitespace-only reasonix reply), the stream so far
        # carries zero answer and no error — the workflow lane comes back silently
        # empty. Emit an explicit text block so the lane surfaces the problem instead
        # of looking like a clean empty success. Off via
        # CLAUDE_REASONIX_GATEWAY_HOLLOW_GUARD=0.
        if emitted_real == 0 and os.getenv("CLAUDE_REASONIX_GATEWAY_HOLLOW_GUARD", os.getenv("CLAUDE_CODEX_GATEWAY_HOLLOW_GUARD", "1")).lower() in {"1", "true", "yes", "on"}:
            self.send_sse_event(
                "content_block_start",
                {"type": "content_block_start", "index": next_index, "content_block": {"type": "text", "text": ""}},
            )
            self.send_sse_event(
                "content_block_delta",
                {"type": "content_block_delta", "index": next_index, "delta": {"type": "text_delta",
                 "text": "[reasonix lane returned no content — the task may be too large for one "
                         "lane or the model produced nothing. Split this into smaller lanes and retry.]"}},
            )
            self.send_sse_event("content_block_stop", {"type": "content_block_stop", "index": next_index})

        self.send_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": message.get("stop_reason"), "stop_sequence": None},
                "usage": {"output_tokens": message.get("usage", {}).get("output_tokens", 0)},
            },
        )
        self.send_sse_event("message_stop", {"type": "message_stop"})

    def send_openai_sse_data(self, data: Any) -> None:
        self.wfile.write(b"data: ")
        if isinstance(data, str):
            self.wfile.write(data.encode("utf-8"))
        else:
            self.wfile.write(json_bytes(data))
        self.wfile.write(b"\n\n")
        self.wfile.flush()

    def send_openai_sse_response(self, response: JSON) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        self.write_openai_sse_response_body(response)

    def send_openai_sse_response_lazy(self, producer: Any) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        # The OpenAI /v1/chat/completions lazy path intentionally keeps the bare
        # ": keepalive" comment (no on_keepalive). The deep-research workflow routes
        # through the Anthropic /v1/messages path, which is where the workflow
        # watchdog heartbeat is required. Revisit if CLAUDE_REASONIX_GATEWAY_BACKEND ever
        # routes workflow subagents through chat/completions.
        try:
            response = self.wait_for_stream_response(producer)
        except Exception as exc:
            self.send_openai_sse_data({"error": {"type": "api_error", "message": str(exc)}})
            self.send_openai_sse_data("[DONE]")
            return
        self.write_openai_sse_response_body(response)

    def write_openai_sse_response_body(self, response: JSON) -> None:
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        base = {
            "id": response.get("id") or f"chatcmpl_{uuid4().hex}",
            "object": "chat.completion.chunk",
            "created": int(response.get("created") or time.time()),
            "model": response.get("model"),
        }

        first = dict(base)
        first["choices"] = [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
        self.send_openai_sse_data(first)

        text = message.get("content")
        if isinstance(text, str) and text:
            chunk = dict(base)
            chunk["choices"] = [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
            self.send_openai_sse_data(chunk)

        for call in message.get("tool_calls") or []:
            chunk = dict(base)
            chunk["choices"] = [{"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}]
            self.send_openai_sse_data(chunk)

        final = dict(base)
        final["choices"] = [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}]
        self.send_openai_sse_data(final)
        self.send_openai_sse_data("[DONE]")

    def forward_anthropic(self, payload: JSON) -> None:
        upstream_base = env_first("CLAUDE_REASONIX_GATEWAY_ANTHROPIC_BASE_URL", "CLAUDE_CODEX_GATEWAY_ANTHROPIC_BASE_URL", default="https://api.anthropic.com").rstrip("/")
        url = upstream_base + self.path
        headers: dict[str, str] = {"content-type": "application/json"}
        for name in ("anthropic-beta", "anthropic-version", "accept"):
            value = self.headers.get(name)
            if value:
                headers[name] = value

        auth_token = env_first("CLAUDE_REASONIX_GATEWAY_ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODEX_GATEWAY_ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")
        api_key = env_first("CLAUDE_REASONIX_GATEWAY_ANTHROPIC_API_KEY", "CLAUDE_CODEX_GATEWAY_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
        if auth_token:
            headers["authorization"] = f"Bearer {auth_token}"
        elif api_key:
            headers["x-api-key"] = api_key
        else:
            incoming_auth = self.headers.get("authorization")
            incoming_key = self.headers.get("x-api-key")
            if incoming_auth:
                headers["authorization"] = incoming_auth
            if incoming_key:
                headers["x-api-key"] = incoming_key

        req = urllib.request.Request(url, data=json_bytes(payload), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=float(os.getenv("CLAUDE_REASONIX_GATEWAY_TIMEOUT", os.getenv("CLAUDE_CODEX_GATEWAY_TIMEOUT", "600")))) as response:
                body = response.read()
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() in {"connection", "transfer-encoding", "content-encoding"}:
                        continue
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self.send_response(exc.code)
            self.send_header("content-type", exc.headers.get("content-type", "application/json"))
            self.end_headers()
            self.wfile.write(body)


def _keepalive_loop() -> None:
    """Background thread: every interval, re-touch each recently-seen shared prefix
    with a tiny request so DeepSeek's LRU keeps it resident between same-codebase
    workflows. Each ping carries ONLY the stored head (the cacheable shared block) +
    a 1-token ask, so it costs ~one cache-hit-priced request and refreshes recency.
    Best-effort: swallows all errors; never affects real lanes."""
    interval = env_float("CLAUDE_REASONIX_GATEWAY_KEEPALIVE_INTERVAL_SECONDS", "CLAUDE_CODEX_GATEWAY_KEEPALIVE_INTERVAL_SECONDS", default=120.0)
    config = model_registry().get("claude-reasonix-flash", {})
    while True:
        try:
            _time.sleep(max(15.0, interval))
            if not _keepalive_enabled():
                continue
            for _key, head in keepalive_targets():
                try:
                    # A minimal ping: the shared head + a 1-word ask. Hits the warm
                    # prefix, refreshes its LRU recency, returns fast.
                    run_reasonix_acp(head + "\nReply with one word.", config)
                    gateway_trace("keepalive_ping", key=_key[:12])
                except Exception:
                    pass
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Local native-model gateway for claude-reasonix")
    parser.add_argument("--host", default=os.getenv("CLAUDE_REASONIX_GATEWAY_HOST", os.getenv("CLAUDE_CODEX_GATEWAY_HOST", "127.0.0.1")))
    parser.add_argument("--port", type=int, default=int(os.getenv("CLAUDE_REASONIX_GATEWAY_PORT", os.getenv("CLAUDE_CODEX_GATEWAY_PORT", "0"))))
    parser.add_argument("--port-file", default="")
    args = parser.parse_args()

    # Lever C (default off): load any persisted read-summary cache on startup, dropping
    # entries whose file changed since they were cached (mtime-freshness on load, Q10).
    load_read_cache()

    if _keepalive_enabled():
        threading.Thread(target=_keepalive_loop, daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    actual_port = int(server.server_address[1])
    if args.port_file:
        Path(args.port_file).write_text(str(actual_port), encoding="utf-8")
    print(f"claude-reasonix native gateway listening on http://{args.host}:{actual_port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
