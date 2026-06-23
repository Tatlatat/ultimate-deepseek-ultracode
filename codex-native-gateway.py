#!/usr/bin/env python3
"""Small Anthropic Messages-compatible gateway for claude-codex native agents.

The gateway is intentionally local and session-scoped.  The claude-codex
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


JSON = dict[str, Any]
_CODEX_CLI_SEMAPHORE_LOCK = threading.Lock()
_CODEX_CLI_SEMAPHORE: tuple[int, threading.BoundedSemaphore] | None = None


def env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def env_int(*names: str, default: int) -> int:
    raw = env_first(*names, default=str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(*names: str, default: float) -> float:
    raw = env_first(*names, default=str(default))
    try:
        return float(raw)
    except ValueError:
        return default


def gateway_trace(event: str, **fields: Any) -> None:
    if os.getenv("CLAUDE_CODEX_GATEWAY_TRACE", "").lower() not in {"1", "true", "yes", "on"}:
        return
    record = {"time": time.time(), "event": event, **fields}
    print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)


def codex_cli_semaphore() -> threading.BoundedSemaphore:
    global _CODEX_CLI_SEMAPHORE
    limit = max(1, env_int("CLAUDE_CODEX_GATEWAY_CODEX_CONCURRENCY", default=16))
    with _CODEX_CLI_SEMAPHORE_LOCK:
        if _CODEX_CLI_SEMAPHORE is None or _CODEX_CLI_SEMAPHORE[0] != limit:
            _CODEX_CLI_SEMAPHORE = (limit, threading.BoundedSemaphore(limit))
        return _CODEX_CLI_SEMAPHORE[1]


# --- Prefix-prime gate -------------------------------------------------------
# Concurrent fan-out lanes that share a long byte-identical prefix (the common
# review context) otherwise all hit DeepSeek BEFORE its server-side prompt cache
# has stored that prefix from the first lane — so every lane in the burst pays
# the full prefix as a cache MISS (measured: a 28KB-shared 12-lane burst cached
# only ~69%, while the LAST lane, after the prefix warmed, hit 99.7%). The gate
# lets ONE lane per distinct prefix run alone to warm the cache, then releases
# the rest to run concurrently against the now-warm prefix. Keyed by a hash of
# the prompt's leading bytes. Deterministic, controller-independent.
_PRIME_LOCK = threading.Lock()
_PRIME_GATES: dict[str, threading.Event] = {}

# --- Staggered prime serialization -----------------------------------------
# DeepSeek persists a prefix only AFTER a request finishes — so when N lanes of
# one prefix family hit concurrently, the first 2-3 race the persist and all miss
# (measured: 3 early lanes at 65-83% while later ones hit 97-99%). To warm the
# prefix deterministically, the first PRIME_SERIAL lanes of a family take a
# per-key lock and run ONE AT A TIME (each ~persists more of the shared prefix
# before the next); lanes past that window run in parallel against the now-warm
# prefix. Costs ~one-lane latency up front, zero extra tokens.
_PRIME_SERIAL_LOCK = threading.Lock()
_PRIME_SERIAL_COUNTS: dict[str, int] = {}
_PRIME_SERIAL_LOCKS: dict[str, threading.Lock] = {}


def reset_prime_state(key: str) -> None:
    """Test/diagnostic helper — clear the serial counter+lock for a key."""
    with _PRIME_SERIAL_LOCK:
        _PRIME_SERIAL_COUNTS.pop(key, None)
        _PRIME_SERIAL_LOCKS.pop(key, None)


def serial_lock_for(key: str) -> threading.Lock:
    with _PRIME_SERIAL_LOCK:
        lk = _PRIME_SERIAL_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _PRIME_SERIAL_LOCKS[key] = lk
        return lk


def acquire_serial_slot(key: str) -> bool:
    """True if this caller is within the first PRIME_SERIAL lanes of the family and
    must run serially (hold serial_lock_for(key) while running, release when done).
    False if past the window — run in parallel."""
    n = env_int("CLAUDE_CODEX_GATEWAY_PRIME_SERIAL", default=3)
    if n <= 0:
        return False
    with _PRIME_SERIAL_LOCK:
        c = _PRIME_SERIAL_COUNTS.get(key, 0)
        if c >= n:
            return False
        _PRIME_SERIAL_COUNTS[key] = c + 1
        return True


# --- Per-lane loop breaker -------------------------------------------------
# A lane whose model never emits valid JSON gets re-driven turn-by-turn by Claude
# Code, each turn re-feeding history (input 27K->227K, measured). We count repeats
# of the same lane signature; past the threshold, the forced-StructuredOutput path
# returns a schema-valid fallback so the workflow completes instead of looping.
_LANE_LOCK = threading.Lock()
_LANE_COUNTS: dict[str, int] = {}


def register_lane_attempt(prompt: str) -> int:
    key = prefix_prime_key(prompt)
    with _LANE_LOCK:
        _LANE_COUNTS[key] = _LANE_COUNTS.get(key, 0) + 1
        return _LANE_COUNTS[key]


def should_force_fallback(prompt: str) -> bool:
    limit = env_int("CLAUDE_CODEX_GATEWAY_MAX_LANE_RETRIES", default=3)
    if limit <= 0:
        return False
    key = prefix_prime_key(prompt)
    with _LANE_LOCK:
        return _LANE_COUNTS.get(key, 0) >= limit


def prefix_prime_key(prompt: str) -> str:
    """Group lanes for the prime gate by hashing only the LEADING head of the
    prompt (the part lanes actually share), NOT the whole prompt. Measured: real
    fan-out lanes share ~5-8KB (system + shared intro/file head) then diverge into
    per-lane data — hashing 32KB split every lane into its own key, so the gate
    never grouped them. A short head (default 8KB) groups lanes that share that
    leading block, so one primer warms it for the rest. Tunable via
    CLAUDE_CODEX_GATEWAY_PRIME_KEY_HEAD (falls back to the legacy
    CLAUDE_CODEX_GATEWAY_PRIME_HEAD_BYTES if set)."""
    import hashlib
    head = env_int("CLAUDE_CODEX_GATEWAY_PRIME_KEY_HEAD",
                   "CLAUDE_CODEX_GATEWAY_PRIME_HEAD_BYTES", default=4096)
    return hashlib.sha1(prompt[:head].encode("utf-8", "ignore")).hexdigest()[:16]


def acquire_prime_role(prompt: str) -> tuple[bool, threading.Event | None]:
    """Return (is_primer, gate). The first caller for a given prefix is the
    primer (is_primer=True) and MUST call gate.set() when its call completes.
    Later callers get is_primer=False and should wait on the returned gate
    (bounded) before proceeding — by then the prefix is warm."""
    if env_first("CLAUDE_CODEX_GATEWAY_PRIME_GATE", default="1").lower() not in {"1", "true", "yes", "on"}:
        return False, None
    key = prefix_prime_key(prompt)
    with _PRIME_LOCK:
        gate = _PRIME_GATES.get(key)
        if gate is None:
            gate = threading.Event()
            _PRIME_GATES[key] = gate
            return True, gate
        return False, gate


def model_registry() -> dict[str, JSON]:
    return {
        "claude-reasonix-flash": {
            "display_name": os.getenv("CLAUDE_CODEX_REASONIX_DISPLAY_NAME", "claude-reasonix-flash"),
            "provider": "reasonix_cli",
            "target_model": env_first("CLAUDE_CODEX_REASONIX_MODEL", default="deepseek-v4-flash"),
            "reasonix_bin": env_first("REASONIX_BIN", default="reasonix"),
        },
    }


def json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            block_type = block.get("type")
            if block_type == "text":
                parts.append(str(block.get("text", "")))
            elif block_type == "tool_result":
                parts.append(text_from_content(block.get("content")))
            elif block_type == "image":
                parts.append("[image omitted by local gateway]")
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        return "\n".join(part for part in parts if part)
    return str(content)


_BILLING_HEADER_RE = re.compile(r"^x-anthropic-billing-header:[^\n]*\n?", re.MULTILINE)


def normalize_prefix(text: str) -> str:
    """Strip per-request volatile lines from the START of the system prompt so the
    prefix is byte-stable across lanes/sessions and DeepSeek's prompt cache can
    reuse it. The `x-anthropic-billing-header: cc_version=...XXX; ...` line carries
    a rotating version segment (measured: 9d6/94e/ef4/bcd across sessions) at the
    very first bytes, which otherwise busts the cache for the whole leading block.
    It is pure telemetry (version/entrypoint/is_subagent) — reasonix never needs
    it — so removing it is safe and lossless for the worker task."""
    return _BILLING_HEADER_RE.sub("", text)


def anthropic_system_to_text(system: Any) -> str:
    return normalize_prefix(text_from_content(system))


def anthropic_messages_to_openai(payload: JSON) -> list[JSON]:
    messages: list[JSON] = []
    system_text = anthropic_system_to_text(payload.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for item in payload.get("messages", []):
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[JSON] = []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        text_parts.append(str(block))
                        continue
                    if block.get("type") == "text":
                        text_parts.append(str(block.get("text", "")))
                    elif block.get("type") == "tool_use":
                        tool_calls.append(
                            {
                                "id": str(block.get("id") or f"call_{uuid4().hex[:24]}"),
                                "type": "function",
                                "function": {
                                    "name": str(block.get("name") or ""),
                                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                                },
                            }
                        )
            else:
                text_parts.append(text_from_content(content))
            message: JSON = {"role": "assistant", "content": "\n".join(p for p in text_parts if p) or None}
            if tool_calls:
                message["tool_calls"] = tool_calls
            messages.append(message)
            continue

        if role == "user" and isinstance(content, list):
            user_parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    user_parts.append(str(block))
                    continue
                if block.get("type") == "tool_result":
                    if user_parts:
                        messages.append({"role": "user", "content": "\n".join(user_parts)})
                        user_parts = []
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(block.get("tool_use_id") or ""),
                            "content": text_from_content(block.get("content")),
                        }
                    )
                elif block.get("type") == "text":
                    user_parts.append(str(block.get("text", "")))
                elif block.get("type") == "image":
                    user_parts.append("[image omitted by local gateway]")
                else:
                    user_parts.append(json.dumps(block, ensure_ascii=False))
            if user_parts:
                messages.append({"role": "user", "content": "\n".join(user_parts)})
            continue

        if role in {"user", "system"}:
            messages.append({"role": role, "content": text_from_content(content)})

    return messages


def anthropic_tools_to_openai(tools: Any) -> list[JSON] | None:
    if not isinstance(tools, list) or not tools:
        return None
    converted: list[JSON] = []
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": str(tool["name"]),
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return converted or None


def anthropic_tool_choice_to_openai(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind == "none":
        return "none"
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": str(choice["name"])}}
    return None


def openai_response_to_anthropic(data: JSON, requested_model: str) -> JSON:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content: list[JSON] = []

    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})

    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw_args = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except Exception:
            args = {"raw_arguments": raw_args}
        content.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or f"toolu_{uuid4().hex[:24]}"),
                "name": str(function.get("name") or ""),
                "input": args if isinstance(args, dict) else {"value": args},
            }
        )

    finish_reason = choice.get("finish_reason")
    stop_reason = "tool_use" if any(block.get("type") == "tool_use" for block in content) else "end_turn"
    if finish_reason == "length":
        stop_reason = "max_tokens"
    elif finish_reason == "content_filter":
        stop_reason = "stop_sequence"

    usage = data.get("usage") or {}
    return {
        "id": str(data.get("id") or f"msg_{uuid4().hex}"),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        },
    }


def estimate_tokens(payload: Any) -> int:
    return max(1, len(json.dumps(payload, ensure_ascii=False)) // 4)


def provider_chat_payload(payload: JSON, config: JSON) -> JSON:
    request: JSON = {
        "model": config["target_model"],
        "messages": anthropic_messages_to_openai(payload),
    }
    max_tokens = payload.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        request[str(config.get("max_tokens_param") or "max_tokens")] = max_tokens

    tools = anthropic_tools_to_openai(payload.get("tools"))
    if tools:
        request["tools"] = tools
    tool_choice = anthropic_tool_choice_to_openai(payload.get("tool_choice"))
    if tool_choice is not None:
        request["tool_choice"] = tool_choice

    for field in ("temperature", "top_p", "stop"):
        if field in payload:
            request[field] = payload[field]

    reasoning = env_first("CLAUDE_CODEX_GATEWAY_REASONING_EFFORT")
    if reasoning and config.get("provider") == "openai":
        request["reasoning_effort"] = reasoning
    service_tier = env_first("CLAUDE_CODEX_GATEWAY_SERVICE_TIER")
    if service_tier and config.get("provider") == "openai":
        request["service_tier"] = service_tier

    return request


def call_openai_compatible(payload: JSON, requested_model: str, config: JSON) -> JSON:
    if os.getenv("CLAUDE_CODEX_GATEWAY_MOCK", "").lower() in {"1", "true", "yes", "on"}:
        return {
            "id": f"msg_{uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": requested_model,
            "content": [
                {
                    "type": "text",
                    "text": f"mock {requested_model} response for {text_from_content((payload.get('messages') or [{}])[-1].get('content'))}",
                }
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": estimate_tokens(payload), "output_tokens": 12},
        }

    if config.get("provider") == "reasonix_cli":
        messages = anthropic_messages_to_openai(payload)
        prompt = openai_messages_to_prompt(messages, payload.get("tools"))
        register_lane_attempt(prompt)
        if os.getenv("CLAUDE_CODEX_GATEWAY_STRUCTURED_DEBUG", "").lower() in {"1", "true", "yes", "on"}:
            try:
                _dd = Path(env_first("CLAUDE_CODEX_FLEET_HOME",
                    default=os.path.dirname(os.path.abspath(__file__)))) / "runtime"
                _dd.mkdir(parents=True, exist_ok=True)
                with open(_dd / "structured-debug.jsonl", "a", encoding="utf-8") as _df:
                    _df.write(json.dumps({
                        "ts": _time.time(), "path": "messages-entry",
                        "tool_names": tool_names_from_payload(payload),
                        "tool_choice": payload.get("tool_choice"),
                        "prompt_has_schema_instr": "STRUCTURED OUTPUT REQUIREMENT" in prompt,
                        "prompt_tail": prompt[-600:],
                    }, ensure_ascii=False) + "\n")
            except Exception:
                pass
        text, usage = run_reasonix_acp(prompt, config)
        gateway_trace("reasonix_acp_response", model=requested_model,
                      cost=usage.get("reasonix_cost_usd"), cache=usage.get("reasonix_cache_pct"))
        ledger = env_first(
            "CLAUDE_CODEX_REASONIX_COST_LEDGER",
            default=str(Path(env_first("CLAUDE_CODEX_FLEET_HOME",
                                       default=os.path.dirname(os.path.abspath(__file__)))) / "runtime" / "reasonix-cost.jsonl"),
        )
        append_reasonix_cost(
            ledger, usage,
            cwd=env_first("CLAUDE_CODEX_GATEWAY_CODEX_CWD", default=os.getcwd()),
            model=str(config.get("target_model") or ""),
            claude_equiv=usage.get("reasonix_claude_equiv_usd"),
        )
        # Dynamic-Workflow agent({schema}) lanes pass a StructuredOutput tool and
        # expect the subagent to RETURN A tool_use, not prose. reasonix/DeepSeek
        # emits the JSON as text (the prompt instruction tells it to), so the
        # workflow harness saw "completed without calling StructuredOutput" and
        # failed the lane. When such a tool was requested AND the model produced a
        # parseable JSON object, wrap it as a StructuredOutput tool_use so the
        # harness gets the tool-call it requires. Fall back to plain text only when
        # no structured tool was requested or the output isn't valid JSON.
        structured_tool = requested_structured_output_tool(payload)
        if os.getenv("CLAUDE_CODEX_GATEWAY_STRUCTURED_DEBUG", "").lower() in {"1", "true", "yes", "on"}:
            try:
                _dbg_dir = Path(env_first("CLAUDE_CODEX_FLEET_HOME",
                    default=os.path.dirname(os.path.abspath(__file__)))) / "runtime"
                _dbg_dir.mkdir(parents=True, exist_ok=True)
                _parsed = parse_json_object_from_text(text) if structured_tool else None
                with open(_dbg_dir / "structured-debug.jsonl", "a", encoding="utf-8") as _df:
                    _df.write(json.dumps({
                        "ts": _time.time(),
                        "tool_names": tool_names_from_payload(payload),
                        "structured_tool": structured_tool,
                        "tool_choice": payload.get("tool_choice"),
                        "text_len": len(text),
                        "text_head": text[:400],
                        "parsed_ok": _parsed is not None,
                    }, ensure_ascii=False) + "\n")
            except Exception:
                pass
        if structured_tool:
            tool_input = parse_json_object_from_text(text)
            if tool_input is None:
                # DeepSeek sometimes narrates ("results returned via StructuredOutput")
                # instead of emitting JSON. When the caller FORCED this tool via
                # tool_choice, OR when this lane has looped past the retry limit, the
                # lane MUST still get a StructuredOutput tool_use or the workflow
                # aborts/loops. Synthesize a schema-valid object so the lane completes.
                forced = _tool_choice_forces(payload, structured_tool)
                looping = should_force_fallback(prompt)
                if forced or looping:
                    if looping:
                        gateway_trace("lane_loop_break", model=requested_model,
                                      retries=env_int("CLAUDE_CODEX_GATEWAY_MAX_LANE_RETRIES", default=3))
                    tool_input = structured_timeout_fallback(
                        payload.get("tools"), structured_tool,
                        "schema-valid fallback (model narrated or lane looped)",
                    )
            if tool_input is not None:
                return anthropic_tool_use_response(requested_model, structured_tool, tool_input, usage)
        return anthropic_end_turn_response(requested_model, usage, text=text)

    raise GatewayError(400, "unsupported_provider", f"unsupported provider: {config.get('provider')!r}; this gateway serves only claude-reasonix-flash")


def tool_schema_entries(tools: Any) -> list[JSON]:
    if not isinstance(tools, list):
        return []

    entries: list[JSON] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict):
            name = str(function.get("name") or "")
            description = str(function.get("description") or tool.get("description") or "")
            parameters = function.get("parameters")
        else:
            name = str(tool.get("name") or "")
            description = str(tool.get("description") or "")
            parameters = tool.get("input_schema") or tool.get("parameters")

        if not name:
            continue
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        entries.append({"name": name, "description": description, "schema": parameters})
    return entries


def schema_type(schema: JSON) -> str:
    raw_type = schema.get("type")
    if isinstance(raw_type, list):
        for item in raw_type:
            if item != "null":
                return str(item)
        return str(raw_type[0]) if raw_type else ""
    return str(raw_type or "")


def fallback_value_from_schema(schema: Any, field_name: str, reason: str) -> Any:
    if not isinstance(schema, dict):
        return reason

    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]

    kind = schema_type(schema)
    properties = schema.get("properties")
    if kind == "object" or isinstance(properties, dict):
        props = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        names = list(props.keys())
        if isinstance(required, list):
            for name in required:
                if isinstance(name, str) and name not in names:
                    names.append(name)
        return {name: fallback_value_from_schema(props.get(name, {"type": "string"}), name, reason) for name in names}

    if kind == "array":
        return []
    if kind == "boolean":
        return False
    if kind in {"integer", "number"}:
        return 0
    if field_name.lower() in {"sourcequality", "quality"}:
        return "unreliable"
    if "date" in field_name.lower():
        return "unknown"
    return reason


def structured_timeout_fallback(tools: Any, tool_name: str, reason: str) -> JSON:
    for entry in tool_schema_entries(tools):
        if entry.get("name") == tool_name:
            fallback = fallback_value_from_schema(entry.get("schema") or {}, "", reason)
            return fallback if isinstance(fallback, dict) else {"error": str(fallback)}
    return {"error": reason}


def structured_output_prompt_instruction(tools: Any) -> str:
    structured_entries = [
        entry for entry in tool_schema_entries(tools)
        if is_structured_output_tool_name(str(entry.get("name") or ""))
    ]
    if not structured_entries:
        return ""

    blocks: list[str] = [
        "STRUCTURED OUTPUT REQUIREMENT:",
        (
            "Respond in ONE shot. Your ENTIRE reply must be EXACTLY ONE JSON object matching the schema "
            "below and NOTHING else — no prose, no markdown fences, no tool-call narration, no commentary "
            "before or after, and do NOT attempt to run shell/Bash commands or call any tool (you cannot; "
            "the embedded commands are context only). Do NOT write sentences like 'returned via "
            "StructuredOutput'. Emit the raw JSON object directly as your whole answer; this gateway "
            "converts that JSON into the StructuredOutput tool call for the caller."
        ),
        (
            "Match the schema exactly: use the exact property names, include every required key, "
            "use only literal enum values, and do not wrap the result in extra keys unless the schema requires them. "
            "Base the content on the task and any data already present in the prompt; if you cannot determine "
            "a value, use a best-effort value or an empty array — never reply with prose."
        ),
    ]
    for entry in structured_entries:
        if entry.get("description"):
            blocks.append(f"Tool {entry['name']} description: {entry['description']}")
        blocks.append(f"Tool {entry['name']} JSON schema:")
        blocks.append(json.dumps(entry.get("schema") or {}, ensure_ascii=False, indent=2, sort_keys=True))
    return "\n".join(blocks)


def _schema_has_nested_array_of_objects(schema: Any) -> bool:
    """True if the JSON schema contains an array whose items are objects (a
    nested structure DeepSeek-flash struggles to emit in one shot)."""
    if not isinstance(schema, dict):
        return False
    props = schema.get("properties")
    if isinstance(props, dict):
        for v in props.values():
            if isinstance(v, dict) and v.get("type") == "array":
                items = v.get("items")
                if isinstance(items, dict) and items.get("type") == "object":
                    return True
            if _schema_has_nested_array_of_objects(v):
                return True
    items = schema.get("items")
    if isinstance(items, dict) and _schema_has_nested_array_of_objects(items):
        return True
    return False


# A lane is a genuine SYNTHESIZE/merge step (where map-reduce belongs) only when its
# prompt is about merging MANY already-collected items into one structured result.
# A READER lane (read these files and report) ALSO carries a nested schema + a long
# prompt, so size+schema alone misclassifies readers as heavy-synthesis and wrongly
# injects the map-reduce skill into them. Gate on explicit synthesize intent so the
# skill fires ONLY in the Synthesize phase.
_SYNTHESIS_INTENT_RE = re.compile(
    r"\b(synthe|merge|combine|aggregate|consolidate|reduce|rank|dedup|"
    r"into one|into a single|across (the |all )?(items|findings|claims|sources|results)|"
    r"the following (items|findings|claims|sources|results))",
    re.IGNORECASE,
)
# A reader lane is the opposite — it ingests source material rather than merging it.
_READER_INTENT_RE = re.compile(
    r"\b(read (the|these|all)|read:|open the file|inspect the (file|repo|code)|"
    r"use webfetch|fetch (the|this) (page|url|source)|enumerate|list what'?s in)",
    re.IGNORECASE,
)


def is_synthesis_prompt(prompt_text: str) -> bool:
    """True when the prompt's intent is to MERGE many items (a synthesize step),
    not to READ source material. Reader-intent wins ties so we never misfire the
    map-reduce skill into a file/web reader lane."""
    if not prompt_text:
        return False
    if _READER_INTENT_RE.search(prompt_text) and not _SYNTHESIS_INTENT_RE.search(prompt_text):
        return False
    return bool(_SYNTHESIS_INTENT_RE.search(prompt_text))


def is_heavy_synthesis(tools: Any, prompt_len: int, prompt_text: str = "") -> bool:
    """A forced StructuredOutput whose schema is nested, whose prompt is large, AND
    whose intent is genuinely to SYNTHESIZE/merge many items is a 'heavy synthesis'
    lane that flash loops on — route it to the map-reduce skill. The synthesis-intent
    gate keeps the skill OUT of reader lanes (which also have nested schemas + long
    prompts). Disabled by CLAUDE_CODEX_GATEWAY_MAPREDUCE_SYNTHESIS=0."""
    if os.getenv("CLAUDE_CODEX_GATEWAY_MAPREDUCE_SYNTHESIS", "1").lower() not in {"1", "true", "yes", "on"}:
        return False
    min_len = env_int("CLAUDE_CODEX_GATEWAY_MAPREDUCE_MIN_PROMPT", default=20000)
    if prompt_len < min_len:
        return False
    # Map-reduce is a Synthesize-phase tool only. A reader lane must never get it.
    if not is_synthesis_prompt(prompt_text):
        return False
    for entry in tool_schema_entries(tools):
        if is_structured_output_tool_name(str(entry.get("name") or "")):
            if _schema_has_nested_array_of_objects(entry.get("schema")):
                return True
    return False


def mapreduce_directive() -> str:
    return (
        "\n\n========================================\n"
        "MANDATORY FIRST ACTION — DO THIS BEFORE ANYTHING ELSE:\n"
        "This synthesis is too large to answer in one turn (it overflows and breaks "
        "the JSON). You MUST delegate it. Your VERY FIRST tool call must be exactly:\n"
        "  run_skill({\"name\": \"map-reduce-synthesis\", \"arguments\": \"<paste the ENTIRE task and item block from above here>\"})\n"
        "Do NOT try to write the JSON yourself. Do NOT summarize the items yourself. "
        "Call run_skill now with the full task as `arguments`, wait for its JSON result, "
        "and return that JSON object verbatim as your answer. The skill 'map-reduce-synthesis' "
        "is in your pinned Skills index.\n"
        "========================================"
    )


def context_budget_directive() -> str:
    """A lane-invariant guard that keeps a worker lane LEAN without a hard read cap.
    Root cause of the 75-80% cache + slow lanes (measured): a lane read 833 files /
    ran 659 commands, ballooning its prompt to 532K tokens — every fresh file is
    uncached content, so cache craters and flash slows. A HARD cap is wrong: a lane
    that genuinely needs 50 files would be killed, and flash (acp, no subagentRunner)
    cannot self-split an oversized task. So this guard does NOT cap — it tells the
    lane to work in a targeted way AND to FLAG when the task is too big to do well in
    one lane, so the work surfaces for decomposition instead of being silently
    crammed. The real fix for oversized work is finer decomposition at the controller
    (see system-prompt-reasonix.md). Byte-identical across lanes (prefix-stable).
    Off via CLAUDE_CODEX_GATEWAY_CONTEXT_GUARD=0."""
    if os.getenv("CLAUDE_CODEX_GATEWAY_CONTEXT_GUARD", "1").lower() not in {"1", "true", "yes", "on"}:
        return ""
    return (
        "WORK LEAN (this directly controls cost and speed — but never skip work the "
        "task actually requires):\n"
        "- Use targeted search (grep/glob for the exact symbol) before reading whole "
        "files or whole directories. Read what the task needs — no more, no less.\n"
        "- Keep a running summary in your own words; do not re-read files you already "
        "read or hold raw file dumps you no longer need.\n"
        "- If this task is genuinely too large to do well in ONE lane (it would need "
        "to read very many files or explore broadly), say so explicitly in your "
        "answer and describe how it should be split into smaller lanes — do NOT try "
        "to cram all of it into this single lane. Right-sized lanes are faster, "
        "cheaper, and more accurate.\n"
    )


def _tool_choice_forces(payload: JSON, tool_name: str) -> bool:
    """True when the caller forced this exact tool via tool_choice (Anthropic
    {type:'tool',name} or OpenAI {type:'function',function:{name}}) or via a
    blanket 'required'/'any'/{type:'any'} choice. A forced choice means the lane
    cannot proceed without a tool_use, so the gateway must guarantee one."""
    choice = payload.get("tool_choice")
    if isinstance(choice, str):
        return choice in {"required", "any"}
    if isinstance(choice, dict):
        ctype = str(choice.get("type") or "")
        if ctype in {"any", "required"}:
            return True
        name = tool_name_from_schema(choice)
        return bool(name) and is_structured_output_tool_name(name) and (
            not tool_name or name == tool_name
        )
    return False


def openai_messages_to_prompt(messages: list[JSON], tools: Any = None) -> str:
    # PREFIX-CACHE STABILITY: the shared, lane-invariant blocks (the leading system
    # message + the tools/structured-output instruction) are emitted FIRST and
    # CONTIGUOUSLY, before any conversation history. Previously the tools
    # instruction was appended LAST, so on multi-turn lanes the per-lane
    # ASSISTANT/USER history sat BETWEEN the shared task and the shared tools
    # instruction — splitting the prefix at ~char 3953 (measured). Hoisting the
    # tools instruction ahead of history makes the shared prefix one long
    # contiguous block identical across lanes, so DeepSeek caches more of it.
    rendered: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = text_from_content(message.get("content"))
        if not content and message.get("tool_calls"):
            content = json.dumps(message.get("tool_calls"), ensure_ascii=False)
        if content:
            rendered.append((role, f"{role.upper()}:\n{content}"))

    # Two kinds of tool instruction with OPPOSITE placement needs:
    #  - generic "tools were provided" note: lane-invariant -> hoist to front for
    #    prefix-cache stability (no effect on output content).
    #  - StructuredOutput schema+requirement: an INSTRUCTION the model must obey on
    #    THIS turn. It must sit LAST, right after the task, or the model answers the
    #    task in prose and ignores the JSON requirement (measured: schema hoisted to
    #    front -> DeepSeek returned prose -> workflow "no StructuredOutput" failure).
    #    Correctness beats the small cache loss for structured lanes.
    structured_instruction = structured_output_prompt_instruction(tools) if tools else ""
    generic_tools_block = None
    if tools and not structured_instruction:
        generic_tools_block = (
            "AVAILABLE CLAUDE CODE TOOL SCHEMAS WERE PROVIDED TO THE MODEL, "
            "but this Codex-backed gateway executes the worker task directly through Codex CLI. "
            "Use Codex CLI repository and shell capabilities instead of returning tool calls."
        )

    # Emit the leading run of system messages first, then the hoistable generic
    # tools note, then everything else (task + per-lane history), and finally the
    # structured-output requirement LAST so it is the freshest instruction.
    lead_system: list[str] = []
    rest: list[str] = []
    seen_non_system = False
    for role, text in rendered:
        if role == "system" and not seen_non_system:
            lead_system.append(text)
        else:
            seen_non_system = True
            rest.append(text)

    parts: list[str] = [*lead_system]
    # Context-budget guard: a lane that can read files/run commands must work within
    # a read budget so it doesn't balloon its own context (measured: 833 reads ->
    # 532K tokens -> 75% cache). Lane-invariant, so it sits at the FRONT with the
    # other shared blocks and does not break the prefix. Only for tool-capable lanes.
    if tools:
        guard = context_budget_directive()
        if guard:
            parts.append(guard)
    if generic_tools_block:
        parts.append(generic_tools_block)
    parts.extend(rest)
    if structured_instruction:
        parts.append(structured_instruction)
        # Heavy nested-schema synthesis on a large prompt: tell reasonix to use the
        # in-engine map-reduce skill instead of looping on a single oversized turn.
        # Appended AFTER the structured instruction so the schema stays LAST.
        assembled_len = sum(len(p) for p in parts)
        if is_heavy_synthesis(tools, assembled_len, "\n\n".join(parts)):
            parts.append(mapreduce_directive())
    return "\n\n".join(parts).strip() or "Complete the requested Codex worker task."


def requested_structured_output_tool(payload: JSON) -> str:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return ""

    tool_names = [
        tool_name_from_schema(tool)
        for tool in tools
        if isinstance(tool, dict)
    ]
    tool_names = [name for name in tool_names if name]
    structured_names = [name for name in tool_names if is_structured_output_tool_name(name)]
    if not structured_names:
        return ""

    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, dict):
        choice_name = tool_name_from_schema(tool_choice)
        if choice_name and not is_structured_output_tool_name(choice_name):
            return ""
        if choice_name:
            return choice_name
    return structured_names[0]


def tool_name_from_schema(tool: JSON) -> str:
    direct = str(tool.get("name") or "")
    if direct:
        return direct
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return ""


def tool_names_from_payload(payload: JSON) -> list[str]:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return []
    return [
        name
        for name in (tool_name_from_schema(tool) for tool in tools if isinstance(tool, dict))
        if name
    ]


def is_structured_output_tool_name(name: str) -> bool:
    normalized = "".join(ch for ch in name.lower() if ch.isalnum())
    return normalized == "structuredoutput" or normalized.endswith("structuredoutput")


def structured_output_success_text(content: Any) -> bool:
    return "structured output provided successfully" in text_from_content(content).lower()


def anthropic_has_successful_structured_output(messages: Any) -> bool:
    if not isinstance(messages, list):
        return False

    structured_use_ids: set[str] = set()
    saw_structured_use = False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if is_structured_output_tool_name(str(block.get("name") or "")):
                saw_structured_use = True
                use_id = str(block.get("id") or "")
                if use_id:
                    structured_use_ids.add(use_id)

    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            if not structured_output_success_text(block.get("content")):
                continue
            use_id = str(block.get("tool_use_id") or "")
            if use_id in structured_use_ids or (saw_structured_use and not use_id):
                return True
    return False


def parse_json_object_from_text(text: str) -> JSON | None:
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    candidates = [raw]
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def anthropic_tool_use_response(requested_model: str, tool_name: str, tool_input: JSON, usage: JSON) -> JSON:
    return {
        "id": f"msg_{uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": [
            {
                "type": "tool_use",
                "id": f"toolu_{uuid4().hex}",
                "name": tool_name,
                "input": tool_input,
            }
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": usage,
    }


def anthropic_end_turn_response(requested_model: str, usage: JSON | None = None, text: str = "") -> JSON:
    return {
        "id": f"msg_{uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 0, "output_tokens": 0},
    }


def weighted_cache(rows: list[JSON]) -> JSON:
    """Weighted cache-hit rate over reasonix-cost rows: sum(in*cache%)/sum(in).
    Only rows with a numeric cache_pct count; returns zeros on empty."""
    total_in = 0
    hit = 0.0
    n = 0
    for r in rows:
        it = r.get("input_tokens") or 0
        cp = r.get("cache_pct")
        if isinstance(cp, (int, float)):
            total_in += it
            hit += it * cp / 100.0
            n += 1
    miss = total_in - hit
    return {
        "weighted_pct": (100.0 * hit / total_in) if total_in else 0.0,
        "total_in": total_in,
        "total_miss": int(round(miss)),
        "n": n,
    }


def classify_miss(rows: list[JSON]) -> JSON:
    """Bucket missed tokens into cold_prefix (fixable by prime gate), loop_inflation
    (big lanes re-fed history, fixable by loop-breaker/map-reduce), and unique_tail
    (genuinely novel content). Heuristic by input size + cache band."""
    cold = loop = unique = 0
    for r in rows:
        it = r.get("input_tokens") or 0
        cp = r.get("cache_pct")
        if not isinstance(cp, (int, float)):
            continue
        miss = int(round(it * (1 - cp / 100.0)))
        if it > 150_000:
            loop += miss
        elif cp < 60 and it < 30_000:
            unique += miss
        else:
            cold += miss
    return {"cold_prefix": cold, "loop_inflation": loop, "unique_tail": unique}


def append_reasonix_cost(ledger_path: str, usage: JSON, cwd: str = "", model: str = "",
                         claude_equiv: float | None = None) -> None:
    """Append one per-lane cost record to the session cost ledger (JSONL).

    Fail-open: a broken/unwritable ledger path must never break a lane.
    The reasonix CLI's own ~/.reasonix/usage.jsonl has session=null and no cwd,
    so it can't attribute cost to a session/project — this ledger adds cwd + ts.
    """
    try:
        record = {
            "ts": time.time(),
            "cost_usd": usage.get("reasonix_cost_usd"),
            "claude_equiv_usd": claude_equiv,
            "cache_pct": usage.get("reasonix_cache_pct"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cwd": cwd,
            "model": model,
        }
        path = Path(ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def summarize_reasonix_cost(ledger_path: str) -> JSON:
    """Aggregate the cost ledger into a summary dict. Missing/empty → zeros."""
    lanes = 0
    total = 0.0
    claude_equiv = 0.0
    in_tok = 0
    out_tok = 0
    cache_vals: list[float] = []
    try:
        with open(ledger_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                lanes += 1
                c = rec.get("cost_usd")
                if isinstance(c, (int, float)):
                    total += float(c)
                ce = rec.get("claude_equiv_usd")
                if isinstance(ce, (int, float)):
                    claude_equiv += float(ce)
                if isinstance(rec.get("input_tokens"), int):
                    in_tok += rec["input_tokens"]
                if isinstance(rec.get("output_tokens"), int):
                    out_tok += rec["output_tokens"]
                cp = rec.get("cache_pct")
                if isinstance(cp, (int, float)):
                    cache_vals.append(float(cp))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    avg_cache = round(sum(cache_vals) / len(cache_vals), 1) if cache_vals else 0.0
    saved = claude_equiv - total
    saved_pct = round(100.0 * saved / claude_equiv, 1) if claude_equiv > 0 else 0.0
    return {
        "lanes": lanes,
        "total_usd": total,
        "claude_equiv_usd": claude_equiv,
        "saved_usd": saved,
        "saved_pct": saved_pct,
        "avg_cache_pct": avg_cache,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "avg_per_lane_usd": round(total / lanes, 6) if lanes else 0.0,
    }


def run_reasonix_acp(prompt: str, config: JSON) -> tuple[str, JSON]:
    # TEST HOOK: simulate reasonix's reply WITHOUT spawning the CLI / hitting
    # DeepSeek, so an e2e test can drive the FULL real path — including the
    # parse-text->StructuredOutput-tool_use and forced-fallback logic in
    # call_openai_compatible, which is exactly where workflow lanes live or die and
    # which the old text-only MOCK mode skipped entirely. Set
    # CLAUDE_CODEX_GATEWAY_MOCK_REASONIX_TEXT to the text reasonix should "return"
    # (e.g. a JSON object, or prose to test the narrate->fallback path).
    _mock_text = os.getenv("CLAUDE_CODEX_GATEWAY_MOCK_REASONIX_TEXT")
    if _mock_text is not None:
        return _mock_text, {
            "input_tokens": max(1, len(prompt) // 4), "output_tokens": max(1, len(_mock_text) // 4),
            "cache_pct": 0.0, "reasonix_cost_usd": 0.0, "reasonix_cache_pct": 0.0,
        }
    import queue as _queue
    reasonix_bin = str(config.get("reasonix_bin") or env_first("REASONIX_BIN", default="reasonix"))
    # reasonix is a Node CLI whose shebang is `env node`. If the gateway was
    # launched with a PATH that lacks the node directory (the fnm multishell dir
    # that also holds the reasonix bin), the spawn dies with
    # "env: node: No such file or directory" and reasonix produces no output —
    # every workflow lane then returns empty text. Prepend the reasonix bin's
    # own directory (which contains node) to the child PATH so `env node`
    # resolves regardless of how the gateway itself was started.
    reasonix_env = dict(os.environ)
    # Session isolation (ROOT CAUSE of erratic fan-out cache): stock reasonix acp
    # persists each spawn's session under a MINUTE-granular name
    # (`acp-${timestampSuffix()}`, 12-char ISO slice) and the CacheFirstLoop ctor
    # loadSessionMessages() RESUMES any same-name session. So every lane spawning in
    # the same wall-clock minute inherits all prior same-minute lanes' full
    # conversations as prior context — measured: in_tok inflates ~+10829 tok/lane,
    # cache swings 60-94% by collision. Default ephemeral sessions (session:null, no
    # load/append) so each stateless fan-out lane is fully isolated. Requires the
    # one-line dist patch that honors REASONIX_ACP_EPHEMERAL_SESSION; kill-switch:
    # set CLAUDE_CODEX_GATEWAY_REASONIX_EPHEMERAL=0 to restore stock behavior.
    if env_first("CLAUDE_CODEX_GATEWAY_REASONIX_EPHEMERAL", default="1") not in {"0", "false", "no", "off"}:
        reasonix_env.setdefault("REASONIX_ACP_EPHEMERAL_SESSION", "1")
    _bin_dir = os.path.dirname(os.path.abspath(reasonix_bin)) if os.path.sep in reasonix_bin else ""
    if _bin_dir and os.path.exists(os.path.join(_bin_dir, "node")):
        _cur_path = reasonix_env.get("PATH", "")
        if _bin_dir not in _cur_path.split(os.pathsep):
            reasonix_env["PATH"] = _bin_dir + (os.pathsep + _cur_path if _cur_path else "")
    model = str(config.get("target_model") or "deepseek-v4-flash")
    effort = env_first("CLAUDE_CODEX_REASONIX_EFFORT", default="high")
    budget = env_first("CLAUDE_CODEX_REASONIX_BUDGET", default="0.05")
    timeout = float(env_first("CLAUDE_CODEX_GATEWAY_CODEX_TIMEOUT", "CODEX_FLEET_TIMEOUT_SECONDS", default="600"))
    cwd = env_first("CLAUDE_CODEX_GATEWAY_CODEX_CWD", default=os.getcwd())
    max_attempts = max(1, env_int("CLAUDE_CODEX_GATEWAY_CODEX_MAX_ATTEMPTS", default=3))
    semaphore = codex_cli_semaphore()

    def _attempt() -> tuple[str, JSON]:
        # acp writes per-turn usage+cost to the --transcript JSONL; that is the
        # ONLY place the real cost/token counts are available (acp mode does NOT
        # print a cost line on stderr the way `reasonix run` does). Use a fresh
        # temp transcript per attempt and read it back after the run.
        transcript_fd, transcript_path = tempfile.mkstemp(prefix="reasonix-acp-", suffix=".jsonl")
        os.close(transcript_fd)
        command = [
            reasonix_bin, "acp",
            "--dir", cwd,
            "--yolo",
            "-m", model,
            "--effort", effort,
            "--budget", budget,
            "--transcript", transcript_path,
        ]
        try:
            proc = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1, cwd=cwd,
                env=reasonix_env,
            )
        except OSError as exc:
            try:
                os.unlink(transcript_path)
            except Exception:
                pass
            raise GatewayError(502, "reasonix_acp_error", f"failed to start reasonix acp: {exc}")
        out_q: _queue.Queue = _queue.Queue()
        text_parts: list[str] = []
        session_id = {"v": None}
        prompt_done = {"v": False}
        stop_reason = {"v": None}
        captured: dict = {"v": None}

        def _read_transcript_cost(path: str) -> dict | None:
            # Poll the transcript for the assistant_final record (cost + usage),
            # which reasonix flushes shortly AFTER stopReason. Returns a dict of
            # {cost, claude_equiv, in_tok, out_tok, cache} or None if not yet present.
            deadline = _time.monotonic() + 2.0
            while True:
                cost = claude_equiv = cache = in_tok = out_tok = None
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                            except Exception:
                                continue
                            if isinstance(rec.get("cost"), (int, float)):
                                cost = (cost or 0.0) + float(rec["cost"])
                            if isinstance(rec.get("claudeEquivUsd"), (int, float)):
                                claude_equiv = (claude_equiv or 0.0) + float(rec["claudeEquivUsd"])
                            u = rec.get("usage")
                            if isinstance(u, dict):
                                if isinstance(u.get("prompt_tokens"), int):
                                    in_tok = u["prompt_tokens"]
                                if isinstance(u.get("completion_tokens"), int):
                                    out_tok = u["completion_tokens"]
                                hit = u.get("prompt_cache_hit_tokens")
                                miss = u.get("prompt_cache_miss_tokens")
                                if isinstance(hit, int) and isinstance(miss, int) and (hit + miss) > 0:
                                    cache = round(100.0 * hit / (hit + miss), 1)
                except Exception:
                    pass
                if cost is not None or _time.monotonic() > deadline:
                    return {"cost": cost, "claude_equiv": claude_equiv,
                            "in_tok": in_tok, "out_tok": out_tok, "cache": cache}
                _time.sleep(0.1)

        def send(obj: JSON) -> None:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()

        def reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                out_q.put(msg)
            out_q.put({"__eof__": True})

        threading.Thread(target=reader, daemon=True).start()
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": 1, "clientCapabilities": {}}})
        send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
              "params": {"cwd": cwd, "mcpServers": []}})

        import time as _time
        # FIX 1: bound the handshake phase so a wedged process can't hold a
        # semaphore slot forever.  Resets to the full timeout once the
        # session/prompt (id=3) has been sent.
        deadline = _time.monotonic() + min(timeout, 60.0)
        try:
            while True:
                try:
                    msg = out_q.get(timeout=1.0)
                except Exception:
                    if _time.monotonic() > deadline:
                        proc.kill()
                        raise GatewayError(504, "reasonix_timeout", f"reasonix acp timed out after {timeout:g}s")
                    continue
                if msg.get("__eof__"):
                    break
                if msg.get("id") == 2 and "result" in msg:
                    session_id["v"] = msg["result"].get("sessionId")
                    if not session_id["v"]:
                        proc.kill()
                        raise GatewayError(502, "reasonix_acp_error", "session/new returned no sessionId")
                    send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                          "params": {"sessionId": session_id["v"],
                                     "prompt": [{"type": "text", "text": prompt}]}})
                    # Reset deadline for the full work phase now that handshake is done.
                    deadline = _time.monotonic() + timeout
                elif msg.get("method") == "session/update":
                    upd = (msg.get("params") or {}).get("update") or {}
                    if upd.get("sessionUpdate") == "agent_message_chunk":
                        content = upd.get("content") or {}
                        if isinstance(content, dict) and content.get("type") == "text":
                            text_parts.append(content.get("text", ""))
                elif msg.get("id") == 3 and "result" in msg:
                    stop_reason["v"] = msg["result"].get("stopReason")
                    prompt_done["v"] = True
                    # Poll the transcript for the assistant_final cost record WHILE
                    # the process is still alive — reasonix writes that record a
                    # beat after stopReason, and reaping the process first (in the
                    # finally below) loses it. captured["v"] holds the parsed result.
                    captured["v"] = _read_transcript_cost(transcript_path)
                    break
                elif msg.get("id") == 3 and "error" in msg:
                    proc.kill()
                    raise GatewayError(502, "reasonix_acp_error", msg["error"].get("message", "session/prompt error"))

        finally:
            # Deterministic reap + pipe close on every exit path. Close OUR stdin
            # first (signals EOF so reasonix can finish flushing its transcript and
            # exit on its own), then give it a short grace period to exit cleanly
            # BEFORE terminating. Terminating immediately killed reasonix mid-flush,
            # which lost the cost/usage transcript record ~2/3 of the time.
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)  # graceful: let it flush transcript + exit
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass
            # Read stderr now that the process is dead (won't block).
            # On error/exception paths this is a best-effort capture; the
            # caller receives the GatewayError and we discard stderr_text.
            try:
                _stderr_capture = proc.stderr.read() if proc.stderr else ""
            except Exception:
                _stderr_capture = ""
            # Close all Python-side pipe fds on every exit path.
            for _stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if _stream:
                        _stream.close()
                except Exception:
                    pass

        # _stderr_capture is kept only for error diagnostics; cost/usage come
        # from the transcript JSONL below.
        _ = _stderr_capture

        # Cost/usage were parsed from the transcript WHILE the process was still
        # alive (captured["v"]), because reasonix flushes the assistant_final cost
        # record a beat after stopReason and the reap above would otherwise lose it.
        # Fall back to a fresh read if that path didn't run (e.g. error exit).
        parsed = captured["v"]
        if parsed is None:
            parsed = _read_transcript_cost(transcript_path) or {}
        cost = parsed.get("cost")
        claude_equiv = parsed.get("claude_equiv")
        cache = parsed.get("cache")
        in_tok = parsed.get("in_tok")
        out_tok = parsed.get("out_tok")
        try:
            os.unlink(transcript_path)
        except Exception:
            pass

        text = "".join(text_parts)
        usage = {
            "input_tokens": in_tok if in_tok is not None
            else estimate_tokens({"messages": [{"role": "user", "content": prompt}]}),
            "output_tokens": out_tok if out_tok is not None else max(1, len(text) // 4),
            "reasonix_cost_usd": cost,
            "reasonix_cache_pct": cache,
            "reasonix_claude_equiv_usd": claude_equiv,
        }
        # Prefix-cache diagnostics (opt-in via CLAUDE_CODEX_GATEWAY_PREFIX_TRACE).
        # Logs a rolling sequence of (hash of prompt's first 4k chars, hash of
        # first 32k, full length, cache%) per lane so we can tell post-hoc whether
        # low-cache lanes share a long common prefix with earlier lanes (a
        # prompt-ORDER problem we can fix by stabilising the prefix) or have a
        # genuinely novel prefix (unavoidable cold start). Append-only JSONL; no
        # behavior change. The prompt text itself is NOT logged, only hashes.
        if os.getenv("CLAUDE_CODEX_GATEWAY_PREFIX_TRACE", "").lower() in {"1", "true", "yes", "on"}:
            try:
                import hashlib
                pfx4 = hashlib.sha1(prompt[:4096].encode("utf-8", "ignore")).hexdigest()[:12]
                pfx32 = hashlib.sha1(prompt[:32768].encode("utf-8", "ignore")).hexdigest()[:12]
                # Per-4k-chunk hashes so we can find WHERE two same-family lanes
                # diverge (the chunk index where their hash sequences first differ),
                # and a short text sample of each chunk's HEAD so we can classify the
                # divergent region as source-code (e.g. starts with "def "/"import "/
                # "class "/file-path lines — not shareable) vs an instruction/template
                # block (shareable, just ordered late). Samples are 80 chars, head of
                # the chunk only — enough to classify, not to leak the full prompt.
                chunks = [prompt[i:i + 4096] for i in range(0, min(len(prompt), 131072), 4096)]
                chunk_hashes = [hashlib.sha1(c.encode("utf-8", "ignore")).hexdigest()[:10] for c in chunks]
                chunk_samples = [c[:80] for c in chunks]
                rec = {
                    "ts": _time.time(),
                    "prefix4k": pfx4,
                    "prefix32k": pfx32,
                    "prompt_len": len(prompt),
                    "cache_pct": cache,
                    "in_tok": in_tok,
                    "chunk_hashes": chunk_hashes,
                    "chunk_samples": chunk_samples,
                }
                ledger_dir = Path(env_first(
                    "CLAUDE_CODEX_FLEET_HOME",
                    default=os.path.dirname(os.path.abspath(__file__)))) / "runtime"
                ledger_dir.mkdir(parents=True, exist_ok=True)
                with open(ledger_dir / "prefix-trace.jsonl", "a", encoding="utf-8") as _pf:
                    _pf.write(json.dumps(rec) + "\n")
            except Exception:
                pass
        return text, usage

    # Prefix-prime gate: the first lane of a shared-prefix burst warms DeepSeek's
    # cache alone; later lanes wait (bounded) for that warm-up, then run together.
    is_primer, prime_gate = acquire_prime_role(prompt)
    if os.getenv("CLAUDE_CODEX_GATEWAY_PREFIX_TRACE", "").lower() in {"1", "true", "yes", "on"}:
        try:
            _pdir = Path(env_first("CLAUDE_CODEX_FLEET_HOME",
                default=os.path.dirname(os.path.abspath(__file__)))) / "runtime"
            _pdir.mkdir(parents=True, exist_ok=True)
            with open(_pdir / "prime-trace.jsonl", "a", encoding="utf-8") as _pf:
                _pf.write(json.dumps({
                    "ts": _time.time(),
                    "role": "primer" if is_primer else ("waiter" if prime_gate is not None else "ungated"),
                    "prime_key": prefix_prime_key(prompt),
                    "prompt_len": len(prompt),
                }) + "\n")
        except Exception:
            pass
    # Staggered serialization: the prime gate releases ALL waiters at once when it
    # opens, so the first few still fire concurrently and race the prefix persist
    # (measured: 3 early lanes 65-83% while later lanes 97-99%). To eliminate that,
    # the first PRIME_SERIAL lanes of the family take a per-key lock and run ONE AT
    # A TIME — each finishes and persists more of the shared prefix before the next
    # starts. Lanes past the window skip the lock and run in parallel against the
    # now-warm prefix. The primer is lane 0 of its family, so it holds the slot too;
    # waiters that wake hold subsequent slots and serialize behind it.
    prime_key = prefix_prime_key(prompt)
    serial_slot = acquire_serial_slot(prime_key)
    serial_lock = serial_lock_for(prime_key) if serial_slot else None

    if prime_gate is not None and not is_primer:
        wait_s = env_float("CLAUDE_CODEX_GATEWAY_PRIME_WAIT_SECONDS", default=20.0)
        opened = prime_gate.wait(timeout=wait_s)
        # Post-open grace settle: DeepSeek persists the primed prefix in "seconds"
        # (per its cache docs), so let it finish writing before the waiters fire, or
        # they race the primer and miss the shared prefix. Measured: 1.5s let early
        # waiters race the primer (cache 65-81%); a few seconds lifts them to ~99%.
        # SKIP grace for serial-slot lanes: the per-key serial lock already forces
        # them to run strictly after the prior lane completes + its settle sleep, so
        # an extra grace here only adds dead wall-clock without improving the cache.
        if opened and serial_slot is False:
            grace = env_float("CLAUDE_CODEX_GATEWAY_PRIME_GRACE_SECONDS", default=4.0)
            if grace > 0:
                _time.sleep(min(grace, 15.0))
    if serial_slot and os.getenv("CLAUDE_CODEX_GATEWAY_PREFIX_TRACE", "").lower() in {"1", "true", "yes", "on"}:
        try:
            _sdir = Path(env_first("CLAUDE_CODEX_FLEET_HOME",
                default=os.path.dirname(os.path.abspath(__file__)))) / "runtime"
            _sdir.mkdir(parents=True, exist_ok=True)
            with open(_sdir / "prime-trace.jsonl", "a", encoding="utf-8") as _pf:
                _pf.write(json.dumps({
                    "ts": _time.time(), "event": "serial_slot",
                    "prime_key": prime_key, "prompt_len": len(prompt),
                }) + "\n")
        except Exception:
            pass

    # Retry-on-empty, SCOPED to isolated single-shot lanes only (Option C from the
    # fix-retry-empty-variance workflow, vetted by 2 adversarial lenses). reasonix-flash
    # intermittently returns empty text (~1/15) — a lost task. Retrying recovers it,
    # BUT a retry inside a shared-prefix BURST fires a fresh cold lookup late, after the
    # warm prefix has aged/been displaced, re-reading the full ~19K prompt as a MISS
    # (in_tok ~19135->42967) and dragging the run's weighted cache from 99.7% to ~94%
    # (measured). So retry ONLY when this lane is NOT part of a prime-gate burst
    # (prime_gate is None => an isolated lane with no same-family waiters): that keeps
    # empty-recovery for single subagent calls while review/fan-out bursts never inject
    # a cold mid-burst lane. Env CLAUDE_CODEX_GATEWAY_RETRY_EMPTY: "burst" (default) =
    # isolated-only; "1"/"all" = always (legacy, re-introduces burst variance);
    # "0"/off = never.
    _re = os.getenv("CLAUDE_CODEX_GATEWAY_RETRY_EMPTY", "burst").lower()
    retry_empty_isolated = _re not in {"0", "false", "no", "off"}
    retry_empty_in_burst = _re in {"1", "true", "yes", "on", "all"}

    def _run_attempts() -> tuple[str, JSON]:
        last_exc: Exception | None = None
        last_result: tuple[str, JSON] | None = None
        # Only an isolated lane (no same-family burst) may retry on empty, unless
        # forced on for all. prime_gate is None => isolated.
        may_retry_empty = retry_empty_isolated and (retry_empty_in_burst or prime_gate is None)
        for attempt in range(1, max_attempts + 1):
            try:
                gateway_trace("reasonix_acp_attempt", model=model, attempt=attempt)
                result = _attempt()
            except GatewayError as exc:
                last_exc = exc
                if exc.error_type == "reasonix_timeout":
                    raise
                continue
            if may_retry_empty and not str(result[0]).strip() and attempt < max_attempts:
                gateway_trace("reasonix_acp_empty_retry", model=model, attempt=attempt)
                last_result = result
                continue
            return result
        if last_result is not None:
            return last_result
        if last_exc:
            raise last_exc
        raise GatewayError(502, "reasonix_acp_error", "reasonix acp produced no result")

    def _run_serialized() -> tuple[str, JSON]:
        # A serial-slot lane runs under the per-key lock so only one family member
        # runs at a time; after it completes it sleeps a short settle so DeepSeek
        # persists what this lane just warmed before the next serial lane starts.
        if serial_lock is None:
            return _run_attempts()
        serial_lock.acquire()
        try:
            return _run_attempts()
        finally:
            settle = env_float("CLAUDE_CODEX_GATEWAY_PRIME_SERIAL_SETTLE_SECONDS", default=4.0)
            if settle > 0:
                _time.sleep(min(settle, 15.0))
            serial_lock.release()

    with semaphore:
        try:
            return _run_serialized()
        finally:
            # The primer must release waiters whether it succeeded or failed, so a
            # failed prime can't deadlock the burst. The warmed prefix (if any)
            # stays cached server-side regardless.
            if is_primer and prime_gate is not None:
                prime_gate.set()


def call_openai_chat_completion(payload: JSON, requested_model: str, config: JSON) -> JSON:
    if config.get("provider") == "reasonix_cli":
        # CCR routes every workflow subagent lane through /v1/chat/completions,
        # which lands here. Without this branch reasonix_cli fell through to the
        # api_key check below and 401'd with "needs an API key" — the real cause
        # of "Not logged in" on every workflow lane. Mirror the /v1/messages
        # reasonix path (run_reasonix_acp + cost ledger) but emit OpenAI shape.
        messages = payload.get("messages") or []
        normalized = [item for item in messages if isinstance(item, dict)]
        prompt = openai_messages_to_prompt(normalized, payload.get("tools"))
        register_lane_attempt(prompt)
        text, usage = run_reasonix_acp(prompt, config)
        gateway_trace("reasonix_acp_openai_response", model=requested_model,
                      cost=usage.get("reasonix_cost_usd"), cache=usage.get("reasonix_cache_pct"))
        ledger = env_first(
            "CLAUDE_CODEX_REASONIX_COST_LEDGER",
            default=str(Path(env_first("CLAUDE_CODEX_FLEET_HOME",
                                       default=os.path.dirname(os.path.abspath(__file__)))) / "runtime" / "reasonix-cost.jsonl"),
        )
        append_reasonix_cost(
            ledger, usage,
            cwd=env_first("CLAUDE_CODEX_GATEWAY_CODEX_CWD", default=os.getcwd()),
            model=str(config.get("target_model") or ""),
            claude_equiv=usage.get("reasonix_claude_equiv_usd"),
        )
        prompt_tokens = int(usage.get("prompt_tokens") or estimate_tokens(prompt))
        completion_tokens = int(usage.get("completion_tokens") or max(1, len(text) // 4))
        usage_block = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        # Same StructuredOutput contract as the /v1/messages path: when a workflow
        # agent({schema}) lane (routed here via CCR /v1/chat/completions) asked for
        # a StructuredOutput tool, emit the model's JSON as a tool_calls response so
        # the harness gets the tool-call it requires instead of prose.
        structured_tool = requested_structured_output_tool(payload)
        if os.getenv("CLAUDE_CODEX_GATEWAY_STRUCTURED_DEBUG", "").lower() in {"1", "true", "yes", "on"}:
            try:
                _dbg_dir = Path(env_first("CLAUDE_CODEX_FLEET_HOME",
                    default=os.path.dirname(os.path.abspath(__file__)))) / "runtime"
                _dbg_dir.mkdir(parents=True, exist_ok=True)
                _parsed = parse_json_object_from_text(text) if structured_tool else None
                with open(_dbg_dir / "structured-debug.jsonl", "a", encoding="utf-8") as _df:
                    _df.write(json.dumps({
                        "ts": _time.time(), "path": "chat/completions",
                        "tool_names": tool_names_from_payload(payload),
                        "structured_tool": structured_tool,
                        "tool_choice": payload.get("tool_choice"),
                        "text_len": len(text), "text_head": text[:500],
                        "parsed_ok": _parsed is not None,
                    }, ensure_ascii=False) + "\n")
            except Exception:
                pass
        if structured_tool:
            tool_input = parse_json_object_from_text(text)
            if tool_input is None and (_tool_choice_forces(payload, structured_tool) or should_force_fallback(prompt)):
                # Forced tool but the model narrated instead of emitting JSON, OR the
                # lane looped past the retry limit — synthesize a schema-valid object
                # so the lane completes (mirror of the /v1/messages path).
                tool_input = structured_timeout_fallback(
                    payload.get("tools"), structured_tool,
                    "model did not emit a JSON object; schema-valid fallback used",
                )
            if tool_input is not None:
                return {
                    "id": f"chatcmpl_{uuid4().hex}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": requested_model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": f"call_{uuid4().hex[:24]}",
                                        "type": "function",
                                        "function": {
                                            "name": structured_tool,
                                            "arguments": json.dumps(tool_input, ensure_ascii=False),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": usage_block,
                }
        return {
            "id": f"chatcmpl_{uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": requested_model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage_block,
        }

    raise GatewayError(400, "unsupported_provider", f"unsupported provider: {config.get('provider')!r}; this gateway serves only claude-reasonix-flash")


class GatewayError(Exception):
    def __init__(self, status: int, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.message = message


class ClientGone(Exception):
    """The streaming client disconnected mid-response (BrokenPipe/ConnectionReset).
    Normal, not an error — the handler stops streaming and does NOT try to write an
    error body down the dead socket."""


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-codex-gateway/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.getenv("CLAUDE_CODEX_GATEWAY_QUIET", "1").lower() in {"1", "true", "yes", "on"}:
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
            if os.getenv("CLAUDE_CODEX_GATEWAY_DEBUG", "").lower() in {"1", "true", "yes", "on"}:
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
        interval = max(1.0, float(os.getenv("CLAUDE_CODEX_GATEWAY_STREAM_KEEPALIVE_SECONDS", "10")))
        while True:
            try:
                kind, value = result_queue.get(timeout=interval)
            except queue.Empty:
                # An idle tick. For the Anthropic lazy path we emit a real
                # content_block_delta heartbeat (via on_keepalive) so the Claude
                # Code workflow watchdog sees visible content progress and does not
                # fire its no-progress interrupt while codex exec is still buffering.
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
        # CLAUDE_CODEX_GATEWAY_HOLLOW_GUARD=0.
        if emitted_real == 0 and os.getenv("CLAUDE_CODEX_GATEWAY_HOLLOW_GUARD", "1").lower() in {"1", "true", "yes", "on"}:
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
        # watchdog heartbeat is required. Revisit if CLAUDE_CODEX_CODEX_BACKEND ever
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
        upstream_base = env_first("CLAUDE_CODEX_GATEWAY_ANTHROPIC_BASE_URL", default="https://api.anthropic.com").rstrip("/")
        url = upstream_base + self.path
        headers: dict[str, str] = {"content-type": "application/json"}
        for name in ("anthropic-beta", "anthropic-version", "accept"):
            value = self.headers.get(name)
            if value:
                headers[name] = value

        auth_token = env_first("CLAUDE_CODEX_GATEWAY_ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")
        api_key = env_first("CLAUDE_CODEX_GATEWAY_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
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
            with urllib.request.urlopen(req, timeout=float(os.getenv("CLAUDE_CODEX_GATEWAY_TIMEOUT", "600"))) as response:
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Local native-model gateway for claude-codex")
    parser.add_argument("--host", default=os.getenv("CLAUDE_CODEX_GATEWAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CLAUDE_CODEX_GATEWAY_PORT", "0")))
    parser.add_argument("--port-file", default="")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    actual_port = int(server.server_address[1])
    if args.port_file:
        Path(args.port_file).write_text(str(actual_port), encoding="utf-8")
    print(f"claude-codex native gateway listening on http://{args.host}:{actual_port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
