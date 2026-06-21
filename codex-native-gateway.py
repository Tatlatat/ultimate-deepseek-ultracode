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


def retryable_codex_cli_failure(detail: str) -> bool:
    lowered = detail.lower()
    return (
        "429" in lowered
        or "too many requests" in lowered
        or "rate limit" in lowered
        or "overloaded" in lowered
        or "temporarily unavailable" in lowered
    )


def model_registry() -> dict[str, JSON]:
    codex_backend = os.getenv("CLAUDE_CODEX_CODEX_BACKEND", "codex-cli").strip().lower()
    codex_provider = "openai" if codex_backend in {"openai", "openai-compatible", "api"} else "codex_cli"
    return {
        "claude-codex-pro": {
            "display_name": os.getenv("CLAUDE_CODEX_CODEX_DISPLAY_NAME", "claude-codex-pro"),
            "provider": codex_provider,
            "target_model": env_first("CLAUDE_CODEX_CODEX_MODEL", "CODEX_FLEET_MODEL", default="gpt-5.4"),
            "base_url": env_first("CLAUDE_CODEX_OPENAI_BASE_URL", "OPENAI_BASE_URL", default="https://api.openai.com/v1"),
            "api_key": env_first("CLAUDE_CODEX_OPENAI_API_KEY", "OPENAI_API_KEY"),
            "max_tokens_param": os.getenv("CLAUDE_CODEX_OPENAI_MAX_TOKENS_PARAM", "max_completion_tokens"),
            "codex_bin": env_first("CODEX_BIN", default="codex"),
        },
        "claude-deepseek-pro": {
            "display_name": os.getenv("CLAUDE_CODEX_DEEPSEEK_DISPLAY_NAME", "claude-deepseek-pro"),
            "provider": "deepseek",
            "target_model": env_first("CLAUDE_CODEX_DEEPSEEK_MODEL", "DEEPSEEK_MODEL", default="deepseek-chat"),
            "base_url": env_first("CLAUDE_CODEX_DEEPSEEK_BASE_URL", "DEEPSEEK_BASE_URL", default="https://api.deepseek.com/v1"),
            "api_key": env_first("CLAUDE_CODEX_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
            "max_tokens_param": os.getenv("CLAUDE_CODEX_DEEPSEEK_MAX_TOKENS_PARAM", "max_tokens"),
        },
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


def anthropic_system_to_text(system: Any) -> str:
    return text_from_content(system)


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

    if config.get("provider") == "codex_cli":
        structured_tool = requested_structured_output_tool(payload)
        if structured_tool and anthropic_has_successful_structured_output(payload.get("messages")):
            usage = {"input_tokens": estimate_tokens(payload), "output_tokens": 0}
            gateway_trace(
                "codex_cli_anthropic_structured_output_already_succeeded",
                model=requested_model,
                structured_tool=structured_tool,
            )
            return anthropic_end_turn_response(
                requested_model,
                usage,
                text="Structured output provided successfully.",
            )
        messages = anthropic_messages_to_openai(payload)
        prompt = openai_messages_to_prompt(messages, payload.get("tools"))
        try:
            text, usage = run_codex_cli(prompt, config)
        except GatewayError as exc:
            if structured_tool and exc.error_type == "codex_timeout":
                usage = {"input_tokens": estimate_tokens(payload), "output_tokens": 1}
                structured_input = structured_timeout_fallback(payload.get("tools"), structured_tool, exc.message)
                gateway_trace(
                    "codex_cli_anthropic_structured_timeout_fallback",
                    model=requested_model,
                    structured_tool=structured_tool,
                    reason=exc.message,
                )
                return anthropic_tool_use_response(requested_model, structured_tool, structured_input, usage)
            raise
        structured_input = parse_json_object_from_text(text) if structured_tool else None
        gateway_trace(
            "codex_cli_anthropic_response",
            model=requested_model,
            tool_names=tool_names_from_payload(payload),
            tool_choice_name=tool_name_from_schema(payload.get("tool_choice") or {}) if isinstance(payload.get("tool_choice"), dict) else "",
            structured_tool=structured_tool,
            parsed_json_object=structured_input is not None,
        )
        if structured_tool:
            if structured_input is not None:
                return anthropic_tool_use_response(requested_model, structured_tool, structured_input, usage)
        return {
            "id": f"msg_{uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": requested_model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": usage,
        }

    if config.get("provider") == "reasonix_cli":
        messages = anthropic_messages_to_openai(payload)
        prompt = openai_messages_to_prompt(messages, payload.get("tools"))
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
        return anthropic_end_turn_response(requested_model, usage, text=text)

    api_key = str(config.get("api_key") or "")
    if not api_key:
        raise GatewayError(
            401,
            "authentication_error",
            (
                f"{requested_model} needs an API key. Set OPENAI_API_KEY for claude-codex-pro "
                "or DEEPSEEK_API_KEY for claude-deepseek-pro before starting claude-codex."
            ),
        )

    url = str(config["base_url"]).rstrip("/") + "/chat/completions"
    body = json_bytes(provider_chat_payload(payload, config))
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(os.getenv("CLAUDE_CODEX_GATEWAY_TIMEOUT", "600"))) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GatewayError(exc.code, "provider_error", detail) from exc
    return openai_response_to_anthropic(data, requested_model)


def last_openai_user_text(payload: JSON) -> str:
    for message in reversed(payload.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            return text_from_content(message.get("content"))
    return text_from_content((payload.get("messages") or [{}])[-1].get("content"))


def mock_openai_chat_response(payload: JSON, requested_model: str) -> JSON:
    prompt_tokens = estimate_tokens(payload)
    completion_tokens = 12
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
                    "content": f"mock {requested_model} response for {last_openai_user_text(payload)}",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def provider_openai_chat_payload(payload: JSON, config: JSON) -> JSON:
    request = json.loads(json.dumps(payload))
    request["model"] = config["target_model"]
    request["stream"] = False

    max_tokens_param = str(config.get("max_tokens_param") or "max_tokens")
    if max_tokens_param != "max_tokens" and "max_tokens" in request:
        request[max_tokens_param] = request.pop("max_tokens")

    reasoning = env_first("CLAUDE_CODEX_GATEWAY_REASONING_EFFORT")
    if reasoning and config.get("provider") == "openai":
        request["reasoning_effort"] = reasoning
    service_tier = env_first("CLAUDE_CODEX_GATEWAY_SERVICE_TIER")
    if service_tier and config.get("provider") == "openai":
        request["service_tier"] = service_tier

    return request


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
            "The caller requires a StructuredOutput tool result. This gateway will convert your final "
            "JSON object into that tool call, so return exactly one JSON object and no prose."
        ),
        (
            "Match the schema exactly: use the exact property names, include every required key, "
            "use only literal enum values, and do not wrap the result in extra keys unless the schema requires them."
        ),
    ]
    for entry in structured_entries:
        if entry.get("description"):
            blocks.append(f"Tool {entry['name']} description: {entry['description']}")
        blocks.append(f"Tool {entry['name']} JSON schema:")
        blocks.append(json.dumps(entry.get("schema") or {}, ensure_ascii=False, indent=2, sort_keys=True))
    return "\n".join(blocks)


def openai_messages_to_prompt(messages: list[JSON], tools: Any = None) -> str:
    parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = text_from_content(message.get("content"))
        if not content and message.get("tool_calls"):
            content = json.dumps(message.get("tool_calls"), ensure_ascii=False)
        if content:
            parts.append(f"{role.upper()}:\n{content}")
    if tools:
        structured_instruction = structured_output_prompt_instruction(tools)
        if structured_instruction:
            parts.append(structured_instruction)
        else:
            parts.append(
                "AVAILABLE CLAUDE CODE TOOL SCHEMAS WERE PROVIDED TO THE MODEL, "
                "but this Codex-backed gateway executes the worker task directly through Codex CLI. "
                "Use Codex CLI repository and shell capabilities instead of returning tool calls."
            )
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


def openai_has_successful_structured_output(messages: Any) -> bool:
    if not isinstance(messages, list):
        return False

    structured_call_ids: set[str] = set()
    saw_structured_call = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            if is_structured_output_tool_name(name):
                saw_structured_call = True
                call_id = str(call.get("id") or "")
                if call_id:
                    structured_call_ids.add(call_id)

    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        if not structured_output_success_text(message.get("content")):
            continue
        tool_call_id = str(message.get("tool_call_id") or "")
        if tool_call_id in structured_call_ids or (saw_structured_call and not tool_call_id):
            return True
    return False


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


def openai_tool_call_response(
    requested_model: str,
    tool_name: str,
    tool_input: JSON,
    prompt_tokens: int,
    completion_tokens: int,
) -> JSON:
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
                            "id": f"call_{uuid4().hex}",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_input, ensure_ascii=False, separators=(",", ":")),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def openai_stop_response(
    requested_model: str,
    prompt_tokens: int,
    completion_tokens: int = 0,
    content: str = "",
) -> JSON:
    return {
        "id": f"chatcmpl_{uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def extract_codex_final_text(stdout: str) -> str:
    marker = "\ncodex\n"
    index = stdout.rfind(marker)
    if index == -1 and stdout.startswith("codex\n"):
        index = -1
    if index != -1:
        text = stdout[index + len(marker) :]
    elif stdout.startswith("codex\n"):
        text = stdout[len("codex\n") :]
    else:
        text = stdout
    stop_markers = ["\nhook: Stop", "\ntokens used", "\n__CODEX_"]
    end = len(text)
    for marker_text in stop_markers:
        marker_index = text.find(marker_text)
        if marker_index != -1:
            end = min(end, marker_index)
    return text[:end].strip() or stdout.strip()


def run_codex_cli(prompt: str, config: JSON) -> tuple[str, JSON]:
    codex_bin = str(config.get("codex_bin") or "codex")
    model = str(config.get("target_model") or "gpt-5.4")
    reasoning = env_first("CLAUDE_CODEX_GATEWAY_REASONING_EFFORT", "CODEX_FLEET_REASONING", default="xhigh")
    service_tier = env_first("CLAUDE_CODEX_GATEWAY_SERVICE_TIER", "CODEX_FLEET_SERVICE_TIER", default="fast")
    web_search = env_first("CLAUDE_CODEX_GATEWAY_WEB_SEARCH", "CODEX_FLEET_WEB_SEARCH", default="live")
    sandbox = env_first("CLAUDE_CODEX_GATEWAY_SANDBOX", "CODEX_FLEET_SANDBOX", default="workspace-write")
    approval = env_first("CLAUDE_CODEX_GATEWAY_APPROVAL", "CODEX_FLEET_APPROVAL", default="never")
    timeout = float(env_first("CLAUDE_CODEX_GATEWAY_CODEX_TIMEOUT", "CODEX_FLEET_TIMEOUT_SECONDS", default="600"))
    cwd = env_first("CLAUDE_CODEX_GATEWAY_CODEX_CWD", default=os.getcwd())
    max_attempts = max(1, env_int("CLAUDE_CODEX_GATEWAY_CODEX_MAX_ATTEMPTS", default=3))
    retry_base_seconds = max(0.0, env_float("CLAUDE_CODEX_GATEWAY_CODEX_RETRY_BASE_SECONDS", default=5.0))
    concurrency_limit = max(1, env_int("CLAUDE_CODEX_GATEWAY_CODEX_CONCURRENCY", default=16))
    command = [
        codex_bin,
        "exec",
        "--sandbox",
        sandbox,
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning}"',
        "-c",
        f'service_tier="{service_tier}"',
        "-c",
        "features.fast_mode=true",
        "-c",
        f'web_search="{web_search}"',
        "-c",
        f'approval_policy="{approval}"',
        "--skip-git-repo-check",
        "-",
    ]
    log_dir_raw = env_first("CLAUDE_CODEX_GATEWAY_CODEX_LOG_DIR")
    log_prefix: Path | None = None
    if log_dir_raw:
        request_id = uuid4().hex[:12]
        log_dir = Path(log_dir_raw)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_prefix = log_dir / f"{request_id}-codex-cli"
        (log_prefix.with_suffix(".prompt.txt")).write_text(prompt, encoding="utf-8")
        (log_prefix.with_suffix(".command.json")).write_text(
            json.dumps(
                {
                    "command": command,
                    "cwd": cwd,
                    "model": model,
                    "max_attempts": max_attempts,
                    "concurrency_limit": concurrency_limit,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    completed: subprocess.CompletedProcess[str] | None = None
    semaphore = codex_cli_semaphore()
    with semaphore:
        for attempt in range(1, max_attempts + 1):
            gateway_trace(
                "codex_cli_start",
                model=model,
                attempt=attempt,
                max_attempts=max_attempts,
                concurrency_limit=concurrency_limit,
            )
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=cwd,
                    start_new_session=True,
                )
                try:
                    stdout, stderr = process.communicate(prompt, timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                    except Exception:
                        process.kill()
                    try:
                        stdout, stderr = process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                        except Exception:
                            process.kill()
                        stdout, stderr = process.communicate()
                    exc.stdout = stdout
                    exc.stderr = stderr
                    raise
                completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                if log_prefix is not None:
                    attempt_prefix = log_prefix.with_name(f"{log_prefix.name}.attempt-{attempt}")
                    attempt_prefix.with_suffix(".stdout.txt").write_text(completed.stdout, encoding="utf-8")
                    attempt_prefix.with_suffix(".stderr.txt").write_text(completed.stderr, encoding="utf-8")
                    log_prefix.with_suffix(".stdout.txt").write_text(completed.stdout, encoding="utf-8")
                    log_prefix.with_suffix(".stderr.txt").write_text(completed.stderr, encoding="utf-8")
            except subprocess.TimeoutExpired as exc:
                if log_prefix is not None:
                    stdout_text = exc.stdout or ""
                    stderr_text = exc.stderr or ""
                    log_prefix.with_suffix(".stdout.txt").write_text(str(stdout_text), encoding="utf-8")
                    log_prefix.with_suffix(".stderr.txt").write_text(
                        f"Timed out after {timeout:g}s\n{stderr_text}",
                        encoding="utf-8",
                    )
                raise GatewayError(504, "codex_timeout", f"codex exec timed out after {timeout:g}s") from exc
            except OSError as exc:
                if log_prefix is not None:
                    log_prefix.with_suffix(".stderr.txt").write_text(f"failed to start codex exec: {exc}\n", encoding="utf-8")
                raise GatewayError(502, "codex_exec_error", f"failed to start codex exec: {exc}") from exc

            if completed.returncode == 0:
                break

            detail = (completed.stderr or completed.stdout or "").strip()
            if len(detail) > 4000:
                detail = detail[-4000:]
            if attempt >= max_attempts or not retryable_codex_cli_failure(detail):
                raise GatewayError(502, "codex_exec_error", f"codex exec exited {completed.returncode}: {detail}")

            delay = retry_base_seconds * attempt
            gateway_trace(
                "codex_cli_retry",
                model=model,
                attempt=attempt,
                max_attempts=max_attempts,
                delay_seconds=delay,
                reason=detail[-500:],
            )
            if delay:
                time.sleep(delay)

    if completed is None:
        raise GatewayError(502, "codex_exec_error", "codex exec did not start")

    text = extract_codex_final_text(completed.stdout)
    usage = {
        "input_tokens": estimate_tokens(prompt),
        "output_tokens": max(1, len(text) // 4),
    }
    return text, usage


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
    import queue as _queue
    reasonix_bin = str(config.get("reasonix_bin") or env_first("REASONIX_BIN", default="reasonix"))
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
        return text, usage

    with semaphore:
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                gateway_trace("reasonix_acp_attempt", model=model, attempt=attempt)
                return _attempt()
            except GatewayError as exc:
                last_exc = exc
                if exc.error_type == "reasonix_timeout":
                    raise
        if last_exc:
            raise last_exc
        raise GatewayError(502, "reasonix_acp_error", "reasonix acp produced no result")


def call_openai_chat_completion(payload: JSON, requested_model: str, config: JSON) -> JSON:
    if os.getenv("CLAUDE_CODEX_GATEWAY_MOCK", "").lower() in {"1", "true", "yes", "on"}:
        return mock_openai_chat_response(payload, requested_model)

    if config.get("provider") == "codex_cli":
        messages = payload.get("messages") or []
        normalized = [item for item in messages if isinstance(item, dict)]
        structured_tool = requested_structured_output_tool(payload)
        if structured_tool and openai_has_successful_structured_output(normalized):
            gateway_trace(
                "codex_cli_openai_structured_output_already_succeeded",
                model=requested_model,
                structured_tool=structured_tool,
            )
            return openai_stop_response(
                requested_model,
                estimate_tokens(payload),
                1,
                "Structured output provided successfully.",
            )
        prompt = openai_messages_to_prompt(normalized, payload.get("tools"))
        try:
            text, usage = run_codex_cli(prompt, config)
        except GatewayError as exc:
            if structured_tool and exc.error_type == "codex_timeout":
                prompt_tokens = estimate_tokens(payload)
                structured_input = structured_timeout_fallback(payload.get("tools"), structured_tool, exc.message)
                gateway_trace(
                    "codex_cli_openai_structured_timeout_fallback",
                    model=requested_model,
                    structured_tool=structured_tool,
                    reason=exc.message,
                )
                return openai_tool_call_response(requested_model, structured_tool, structured_input, prompt_tokens, 1)
            raise
        prompt_tokens = int(usage.get("input_tokens") or estimate_tokens(prompt))
        completion_tokens = int(usage.get("output_tokens") or max(1, len(text) // 4))
        structured_input = parse_json_object_from_text(text) if structured_tool else None
        gateway_trace(
            "codex_cli_openai_response",
            model=requested_model,
            tool_names=tool_names_from_payload(payload),
            tool_choice_name=tool_name_from_schema(payload.get("tool_choice") or {}) if isinstance(payload.get("tool_choice"), dict) else "",
            structured_tool=structured_tool,
            parsed_json_object=structured_input is not None,
        )
        if structured_tool and structured_input is not None:
            return openai_tool_call_response(
                requested_model,
                structured_tool,
                structured_input,
                prompt_tokens,
                completion_tokens,
            )
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
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    api_key = str(config.get("api_key") or "")
    if not api_key:
        raise GatewayError(
            401,
            "authentication_error",
            (
                f"{requested_model} needs an API key. Set OPENAI_API_KEY for claude-codex-pro "
                "or DEEPSEEK_API_KEY for claude-deepseek-pro before starting claude-codex."
            ),
        )

    url = str(config["base_url"]).rstrip("/") + "/chat/completions"
    body = json_bytes(provider_openai_chat_payload(payload, config))
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(os.getenv("CLAUDE_CODEX_GATEWAY_TIMEOUT", "600"))) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GatewayError(exc.code, "provider_error", detail) from exc

    if isinstance(data, dict):
        data["model"] = requested_model
    return data


class GatewayError(Exception):
    def __init__(self, status: int, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.message = message


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
                if payload.get("stream"):
                    config = registry[model]
                    if config.get("provider") == "codex_cli":
                        self.send_openai_sse_response_lazy(
                            lambda: call_openai_chat_completion(payload, model, config)
                        )
                    else:
                        response = call_openai_chat_completion(payload, model, config)
                        self.send_openai_sse_response(response)
                else:
                    response = call_openai_chat_completion(payload, model, registry[model])
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
                    # codex_cli runs a blocking subprocess that can take >180s. The
                    # Claude Code workflow watchdog interrupts an agent() lane at
                    # exactly 180s if it sees no visible content progress. So ALWAYS
                    # take the heartbeat-streaming path for codex_cli, regardless of
                    # the client's stream flag: ~34% of real lanes were sent without
                    # stream=true and died silently at 180s on the old blocking blob
                    # path. The Claude Code client parses the SSE stream fine even
                    # when it did not request stream=true.
                    provider = config.get("provider")
                    if provider in ("codex_cli", "reasonix_cli"):
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
        except GatewayError as exc:
            self.send_error_json(exc)
        except Exception as exc:
            if os.getenv("CLAUDE_CODEX_GATEWAY_DEBUG", "").lower() in {"1", "true", "yes", "on"}:
                traceback.print_exc(file=sys.stderr)
            self.send_error_json(GatewayError(500, "api_error", str(exc)))

    def send_sse_event(self, event: str, data: Any) -> None:
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        self.wfile.write(b"data: ")
        self.wfile.write(json_bytes(data))
        self.wfile.write(b"\n\n")
        self.wfile.flush()

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
                if on_keepalive is not None:
                    on_keepalive()
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
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
        for index, block in enumerate(message.get("content") or [], start=start_index):
            block_type = block.get("type")
            if block_type == "text":
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
