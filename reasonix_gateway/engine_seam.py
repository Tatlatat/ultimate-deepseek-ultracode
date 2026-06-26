"""Engine-seam layer: Anthropic<->OpenAI translation, the cache-critical prompt
assembly (openai_messages_to_prompt / lane_task_text via text), structured-output
helpers, and the reasonix ACP engine bridge (run_reasonix_acp).

PURE MECHANICAL EXTRACTION from reasonix-native-gateway.py — every function body
below is byte-identical to its prior in-gateway form (guarded by
tests/test-engine-seam-byte-identical.py). Imports flow one-directionally DOWN:
env, text, levers, harness, cost. This module MUST NOT import the server."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import time as _time
from pathlib import Path
from typing import Any
from uuid import uuid4

from reasonix_gateway.env import JSON, env_first, env_int, env_float, env_truthy
from reasonix_gateway.text import text_from_content, lane_task_text, json_bytes
from reasonix_gateway.cost import append_reasonix_cost
from reasonix_gateway.harness import (_lane_harness_on, lane_unverified_reply,
    parse_harness_result, harness_lane_reply, lane_acceptance_test)
from reasonix_gateway.levers import (
    preindex_enabled, build_preindex, gateway_trace, reasonix_cli_semaphore,
    record_keepalive_prefix, read_cache_injection_block, populate_read_cache,
    serial_lock_for, acquire_serial_slot,
    register_lane_attempt, should_force_fallback, clear_lane_count,
    prefix_prime_key, acquire_prime_role, normalize_prefix,
    tool_schema_entries, schema_type, is_structured_output_tool_name,
    classify_lane_type, is_heavy_synthesis,
    mapreduce_directive, context_budget_directive,
    output_discipline_directive, output_discipline_budget,
    read_summary_budget, read_lane_summary_instruction,
    overscope_rejection,
)


class GatewayError(Exception):
    def __init__(self, status: int, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.message = message


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

    return request


def call_openai_compatible(payload: JSON, requested_model: str, config: JSON) -> JSON:
    if os.getenv("CLAUDE_REASONIX_GATEWAY_MOCK", os.getenv("CLAUDE_CODEX_GATEWAY_MOCK", "")).lower() in {"1", "true", "yes", "on"}:
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
        record_keepalive_prefix(prompt)
        if os.getenv("CLAUDE_REASONIX_GATEWAY_STRUCTURED_DEBUG", os.getenv("CLAUDE_CODEX_GATEWAY_STRUCTURED_DEBUG", "")).lower() in {"1", "true", "yes", "on"}:
            try:
                _dd = Path(env_first("CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                    default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) / "runtime"
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
        lane_type = classify_lane_type(payload.get("tools"), lane_task_text(messages))
        # Lever F HARD layer (default off): cap output by lane-type budget.
        # Lever A HARD layer (default off): for read lanes, read_summary_budget()
        # returns 512 when READ_SUMMARY is on.  Both levers agree on 512 for read
        # lanes; pick the tighter (smallest) non-None cap so either flag alone or
        # both together yield the same correct cap.
        _f_cap = output_discipline_budget(lane_type)
        _a_cap = read_summary_budget() if lane_type == "read" else None
        _caps = [c for c in (_f_cap, _a_cap) if c is not None]
        _max_out = min(_caps) if _caps else None
        # Lever G (default off): reject lanes whose file scope is too broad.
        _rej = overscope_rejection(lane_task_text(messages),
                                   env_first("CLAUDE_REASONIX_GATEWAY_CWD",
                                             "CLAUDE_CODEX_GATEWAY_CODEX_CWD",
                                             default=os.getcwd()))
        if _rej is not None:
            return anthropic_end_turn_response(requested_model, None, text=_rej)
        # Lever A truncation recovery: when A caps THIS read lane (_a_cap set), an empty
        # result means the model was truncated before answering — retry once at a higher
        # cap. Gated by CLAUDE_REASONIX_GATEWAY_READ_RETRY_HOLLOW (default on when A on).
        _retry_hollow = (_a_cap is not None) and env_truthy(
            "CLAUDE_REASONIX_GATEWAY_READ_RETRY_HOLLOW",
            "CLAUDE_CODEX_GATEWAY_READ_RETRY_HOLLOW", default="1")
        # C3: build harness dict gated by flag (default off -> _harness stays None
        # -> run_reasonix_acp gets harness=None -> request dict byte-identical).
        _harness = None
        if _lane_harness_on():
            _at = lane_acceptance_test(messages)
            if _at:
                _harness = {
                    "acceptanceTest": _at,
                    "budgetUsd": env_float("CLAUDE_REASONIX_GATEWAY_LANE_BUDGET_USD",
                                          "CLAUDE_CODEX_GATEWAY_LANE_BUDGET_USD", default=0.05),
                    "harnessMaxAttempts": env_int("CLAUDE_REASONIX_GATEWAY_LANE_MAX_ATTEMPTS",
                                                  "CLAUDE_CODEX_GATEWAY_LANE_MAX_ATTEMPTS", default=4),
                }
        text, usage = run_reasonix_acp(
            prompt, config, max_output_tokens=_max_out,
            retry_empty_force=_retry_hollow, harness=_harness)
        # C3: fold harness reply BEFORE populate_read_cache / ledger so the short
        # structured reply (not raw shim text) flows onward.
        _hp = parse_harness_result(text)
        if _hp is not None:
            text = harness_lane_reply(_hp)
        # Lever C (default off): cache this lane's summary keyed by the file(s) it
        # read so later lanes on the same codebase reuse it (miss->hit). No-op when
        # the flag is off. Best-effort; never breaks the lane.
        populate_read_cache(prompt, text)
        gateway_trace("reasonix_acp_response", model=requested_model,
                      cost=usage.get("reasonix_cost_usd"), cache=usage.get("reasonix_cache_pct"))
        ledger = env_first(
            "CLAUDE_REASONIX_REASONIX_COST_LEDGER", "CLAUDE_CODEX_REASONIX_COST_LEDGER",
            default=str(Path(env_first("CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                                       default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) / "runtime" / "reasonix-cost.jsonl"),
        )
        append_reasonix_cost(
            ledger, usage,
            cwd=env_first("CLAUDE_REASONIX_GATEWAY_CWD", "CLAUDE_CODEX_GATEWAY_CODEX_CWD", default=os.getcwd()),
            model=str(config.get("target_model") or ""),
            claude_equiv=usage.get("reasonix_claude_equiv_usd"),
            lane_type=lane_type,
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
        if os.getenv("CLAUDE_REASONIX_GATEWAY_STRUCTURED_DEBUG", os.getenv("CLAUDE_CODEX_GATEWAY_STRUCTURED_DEBUG", "")).lower() in {"1", "true", "yes", "on"}:
            try:
                _dbg_dir = Path(env_first("CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                    default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) / "runtime"
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
            if tool_input is not None:
                # Real parseable output: this family is NOT stuck looping, so reset its
                # attempt count (otherwise a past loop poisons fresh healthy lanes).
                clear_lane_count(prompt)
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
                                      retries=env_int("CLAUDE_REASONIX_GATEWAY_MAX_LANE_RETRIES", "CLAUDE_CODEX_GATEWAY_MAX_LANE_RETRIES", default=3))
                    tool_input = structured_timeout_fallback(
                        payload.get("tools"), structured_tool,
                        "schema-valid fallback (model narrated or lane looped)",
                    )
            if tool_input is not None:
                return anthropic_tool_use_response(requested_model, structured_tool, tool_input, usage)
        return anthropic_end_turn_response(requested_model, usage, text=text)

    raise GatewayError(400, "unsupported_provider", f"unsupported provider: {config.get('provider')!r}; this gateway serves only claude-reasonix-flash")


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
            "but this Reasonix-backed gateway executes the worker task directly through Reasonix CLI. "
            "Use Reasonix CLI repository and shell capabilities instead of returning tool calls."
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
    # Lever C (default off): inject cached read-summaries at the FIXED boundary —
    # AFTER the shared/lane-invariant blocks (system + guard + generic-tools note),
    # BEFORE the per-lane task+history (`rest`). The block is byte-deterministic for a
    # given set of referenced files (sorted, fixed-format, normalize_prefix-clean), so
    # two lanes that reference the same cached files share these bytes and the prefix
    # is NOT forked. Built from the per-lane TASK text (`rest`), not `parts`, so the
    # block reflects only the files this lane actually references. Off => zero
    # injection, byte-identical to pre-C (enforced by test-read-cache-bytestable.py).
    cache_block = read_cache_injection_block("\n\n".join(rest))
    if cache_block:
        parts.append(cache_block)
    parts.extend(rest)
    if structured_instruction:
        parts.append(structured_instruction)
        # Heavy nested-schema synthesis on a large prompt: tell reasonix to use the
        # in-engine map-reduce skill instead of looping on a single oversized turn.
        # Appended AFTER the structured instruction so the schema stays LAST.
        assembled_len = sum(len(p) for p in parts)
        if is_heavy_synthesis(tools, assembled_len, "\n\n".join(parts)):
            parts.append(mapreduce_directive())
    # Lever F SOFT layer (default off). Appended LAST — after the task and the
    # structured/summary instruction — so the terse/diff-only directive is the
    # freshest instruction the model reads (correctness beats the tiny cache
    # loss, the same trade-off the structured instruction makes). The HARD layer
    # (output_discipline_budget -> maxOutputTokens) is applied at the call site.
    discipline = output_discipline_directive()
    if discipline:
        parts.append(discipline)
    # Lever A SOFT layer (default off). Appended LAST in the same slot as F's
    # directive. Only fires for read lanes when READ_SUMMARY is on AND no
    # StructuredOutput tool was already injected (mutually exclusive). The HARD
    # layer (read_summary_budget -> maxOutputTokens) is applied at the call site.
    # CLASSIFY FROM THE PER-LANE TASK TEXT (`rest`), NOT the assembled prompt:
    # the injected directives (F's "For edits… NEVER write/apply…", the structured
    # instruction, the cache block) all contain edit/read keywords that would
    # POISON the classifier — making every lane classify as 'edit' and silently
    # disabling F's per-type cap (measured: F's directive flipped read/review
    # lanes to 'edit', so the 512/2048 caps never applied). The task text is what
    # actually determines the lane's intent.
    _task_text = "\n\n".join(rest)
    _a_lane_type = classify_lane_type(tools, _task_text)
    read_summary = read_lane_summary_instruction(_a_lane_type, tools)
    if read_summary:
        parts.append(read_summary)
    return "\n\n".join(parts).strip() or "Complete the requested Reasonix worker task."


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


def retry_cap_for_empty(orig_cap: int | None, was_empty: bool, force: bool) -> int | None:
    """Decide the escalated output cap for an EMPTY-on-truncation retry. Returns the
    new (higher) cap to retry with, or None = do not retry.

    Root cause (measured, real DeepSeek): an A-capped read lane over a LARGE file
    spends its small output cap on tool-calls + reasoning + the file outline and gets
    TRUNCATED before emitting the answer, so the engine returns empty text. Empty rate
    scales with cap tightness (cap 512 ~50% empty, 1024 ~17%, no cap ~0%). Retrying the
    SAME cap is pointless (the budget is the cause); retrying at a HIGHER cap gives the
    model room to finish (verified: recovers 2/2). Only escalates when the lane was
    actually capped (orig_cap not None) and Lever A asked for this (force)."""
    if not force or not was_empty or orig_cap is None:
        return None
    try:
        mult = float(os.getenv(
            "CLAUDE_REASONIX_GATEWAY_READ_RETRY_CAP_MULT",
            os.getenv("CLAUDE_CODEX_GATEWAY_READ_RETRY_CAP_MULT", "2")))
    except (TypeError, ValueError):
        mult = 2.0
    new_cap = int(orig_cap * mult)
    return new_cap if new_cap > orig_cap else None


def run_reasonix_acp(prompt: str, config: JSON, max_output_tokens: int | None = None,
                     retry_empty_force: bool = False, harness: JSON | None = None) -> tuple[str, JSON]:
    # TEST HOOK: simulate reasonix's reply WITHOUT spawning the CLI / hitting
    # DeepSeek, so an e2e test can drive the FULL real path — including the
    # parse-text->StructuredOutput-tool_use and forced-fallback logic in
    # call_openai_compatible, which is exactly where workflow lanes live or die and
    # which the old text-only MOCK mode skipped entirely. Set
    # CLAUDE_REASONIX_GATEWAY_MOCK_REASONIX_TEXT to the text reasonix should "return"
    # (e.g. a JSON object, or prose to test the narrate->fallback path).
    _mock_text = os.getenv("CLAUDE_REASONIX_GATEWAY_MOCK_REASONIX_TEXT", os.getenv("CLAUDE_CODEX_GATEWAY_MOCK_REASONIX_TEXT"))
    # The general GATEWAY_MOCK switch must also short-circuit this path: lanes routed
    # through /v1/chat/completions (provider reasonix_cli) reach run_reasonix_acp,
    # and without a mock here they spawn the real CLI and hang in a CI/test env that
    # has no reasonix. Fall back to a deterministic reply so the full path is tested.
    if _mock_text is None and os.getenv(
        "CLAUDE_REASONIX_GATEWAY_MOCK", os.getenv("CLAUDE_CODEX_GATEWAY_MOCK", "")
    ).lower() in {"1", "true", "yes", "on"}:
        _mock_text = f"mock reasonix response for {prompt[:60]}"
    if _mock_text is not None:
        return _mock_text, {
            "input_tokens": max(1, len(prompt) // 4), "output_tokens": max(1, len(_mock_text) // 4),
            "cache_pct": 0.0, "reasonix_cost_usd": 0.0, "reasonix_cache_pct": 0.0,
        }
    # ENGINE SEAM: run ONE lane through the in-process owner's-fork engine shim
    # (`node engine/run-lane.mjs`) instead of spawning upstream `reasonix acp`.
    # The shim imports the built fork dist, constructs DeepSeekClient +
    # ImmutablePrefix + CacheFirstLoop + buildCodeToolset, drives loop.step() with
    # stream:true + session:undefined (ephemeral), and prints ONE JSON line:
    #   {text, usage:{prompt_tokens, completion_tokens,
    #                 prompt_cache_hit_tokens, prompt_cache_miss_tokens,
    #                 cache_hit_ratio}, cost_usd}
    # We re-map THAT to the gateway's internal usage dict (input_tokens /
    # output_tokens / cache_pct / reasonix_cost_usd / reasonix_cache_pct), which
    # downstream cost/cache logging + the realworld-bench cache metric consume.
    # The shim is JUST the lane producer — the gateway's streaming/heartbeat/
    # prime-gate/keepalive machinery (below + in send_sse_response_lazy) is
    # unchanged. A one-shot subprocess per lane is behaviourally identical to the
    # old per-lane acp spawn; DeepSeek's cache hits come from its server-side
    # prefix cache (same prefix bytes), not from any in-memory engine state.
    #
    # Resolve the install home the same way the gateway resolves its own dir (the
    # gateway lives at <INSTALL_HOME>/reasonix-native-gateway.py), so the shim is
    # at <INSTALL_HOME>/engine/run-lane.mjs.
    install_home = env_first(
        "CLAUDE_REASONIX_FLEET_INSTALL_HOME", "CLAUDE_CODEX_FLEET_INSTALL_HOME",
        default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    shim_path = os.path.join(install_home, "engine", "run-lane.mjs")
    node_bin = env_first("CLAUDE_REASONIX_NODE_BIN", "NODE_BIN", default="node")
    model = str(config.get("target_model") or "deepseek-v4-flash")
    effort = env_first("CLAUDE_REASONIX_REASONIX_EFFORT", "CLAUDE_CODEX_REASONIX_EFFORT", default="high")
    budget = env_first("CLAUDE_REASONIX_REASONIX_BUDGET", "CLAUDE_CODEX_REASONIX_BUDGET", default="0.05")
    timeout = float(env_first("CLAUDE_REASONIX_GATEWAY_TIMEOUT", "CLAUDE_CODEX_GATEWAY_CODEX_TIMEOUT", "REASONIX_FLEET_TIMEOUT_SECONDS", default="600"))
    cwd = env_first("CLAUDE_REASONIX_GATEWAY_CWD", "CLAUDE_CODEX_GATEWAY_CODEX_CWD", default=os.getcwd())
    # Lever D — pre-index (default OFF). The gateway is the SOLE build trigger:
    # build the semantic index ONCE per codebase here, before any lane spawns, so
    # per-lane shims only check `indexCompatible()` read-only (no JSONL append
    # race). FAIL-OPEN: build_preindex never raises and is a no-op when PREINDEX is
    # off or no embedding model is reachable — the lane proceeds either way.
    if preindex_enabled():
        try:
            build_preindex(cwd)
        except Exception as _preindex_exc:  # belt-and-suspenders: must never block lanes
            gateway_trace("preindex_fail_open", reason="unexpected", error=str(_preindex_exc))
    max_attempts = max(1, env_int("CLAUDE_REASONIX_GATEWAY_MAX_ATTEMPTS", "CLAUDE_CODEX_GATEWAY_CODEX_MAX_ATTEMPTS", default=3))
    max_iter = max(1, env_int("CLAUDE_REASONIX_GATEWAY_MAX_ITER_PER_TURN", "CLAUDE_CODEX_GATEWAY_MAX_ITER_PER_TURN", default=50))
    semaphore = reasonix_cli_semaphore()
    # The lane system prompt: the gateway prepends the role/system text into the
    # prompt today (openai_messages_to_prompt builds a single prompt string), so
    # the shim's `system` is empty and the full instruction rides in `prompt` —
    # preserving the exact prefix bytes DeepSeek caches. An explicit override is
    # available for callers that want to split system out.
    system_text = str(config.get("system") or os.getenv("CLAUDE_REASONIX_LANE_SYSTEM", ""))

    # The shim is `node`; if the gateway was launched with a stripped PATH that
    # lacks the node dir, propagate the reasonix-bin dir (which historically holds
    # node) so `node` resolves regardless of how the gateway was started. Honor
    # REASONIX_ENGINE_DIST + DeepSeek auth via the child env.
    shim_env = dict(os.environ)
    # When this lane runs with the harness engaged (gateway flag on + an
    # ACCEPTANCE_TEST line present), turn ON the shim's harness gate in the child
    # env so the single gateway flag activates the whole chain (the shim gates its
    # retry loop on its OWN REASONIX_LANE_HARNESS). Only set when engaged — when the
    # harness is off this is never touched, so the child env is byte-identical.
    if harness:
        shim_env["REASONIX_LANE_HARNESS"] = "1"
    _reasonix_bin = env_first("REASONIX_BIN", default="")
    _bin_dir = os.path.dirname(os.path.abspath(_reasonix_bin)) if (_reasonix_bin and os.path.sep in _reasonix_bin) else ""
    if _bin_dir and os.path.exists(os.path.join(_bin_dir, "node")):
        _cur_path = shim_env.get("PATH", "")
        if _bin_dir not in _cur_path.split(os.pathsep):
            shim_env["PATH"] = _bin_dir + (os.pathsep + _cur_path if _cur_path else "")
    # Resolve the engine dist (the built fork). Default to the bundled vendor copy
    # next to the install home if not explicitly set; the shim has its own
    # fallback too, but setting it here keeps the resolution observable.
    if not shim_env.get("REASONIX_ENGINE_DIST"):
        _vendored = os.path.join(install_home, "vendor", "reasonix-engine", "dist", "index.js")
        if os.path.exists(_vendored):
            shim_env["REASONIX_ENGINE_DIST"] = _vendored

    def _attempt(cap_override: int | None = None) -> tuple[str, JSON]:
        request = {
            "prompt": prompt,
            "system": system_text,
            "rootDir": cwd,
            "model": model,
            "maxIterPerTurn": max_iter,
            # carried for parity/observability; the shim ignores unknown fields.
            "effort": effort,
            "budget": budget,
        }
        # C3: forward harness fields to the shim ONLY when harness is provided.
        # When harness is None (default, flag off) the request dict above is
        # byte-identical to the pre-harness baseline — byte-inert guarantee.
        if harness:
            request["acceptanceTest"] = harness["acceptanceTest"]
            request["budgetUsd"] = harness["budgetUsd"]
            request["harnessMaxAttempts"] = harness["harnessMaxAttempts"]
        # cap_override lets the empty-on-truncation retry re-run at a higher cap.
        # Sentinel: cap_override==0 means EXPLICITLY uncapped (omit maxOutputTokens);
        # None means "use the lane's original cap".
        if cap_override == 0:
            _cap = None
        else:
            _cap = cap_override if cap_override is not None else max_output_tokens
        if _cap is not None:
            request["maxOutputTokens"] = _cap
        try:
            proc = subprocess.Popen(
                [node_bin, shim_path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, cwd=cwd, env=shim_env,
            )
        except OSError as exc:
            raise GatewayError(502, "reasonix_acp_error", f"failed to start engine shim: {exc}")
        try:
            stdout_text, stderr_text = proc.communicate(
                input=json.dumps(request) + "\n", timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.communicate(timeout=2)
            except Exception:
                pass
            _mk = lane_unverified_reply(f"engine shim timed out after {timeout:g}s")
            if _mk:
                return _mk, {"input_tokens": 0, "output_tokens": estimate_tokens({"text": _mk}),
                             "cache_pct": None, "reasonix_cost_usd": 0.0,
                             "reasonix_cache_pct": None, "reasonix_claude_equiv_usd": None}
            raise GatewayError(504, "reasonix_timeout", f"engine shim timed out after {timeout:g}s")
        if proc.returncode != 0:
            detail = (stderr_text or "").strip()[:500] or f"engine shim exited {proc.returncode}"
            raise GatewayError(502, "reasonix_acp_error", f"engine shim failed: {detail}")

        # The shim prints ONE JSON line on stdout. Parse the last non-empty line.
        out_line = ""
        for line in (stdout_text or "").splitlines():
            if line.strip():
                out_line = line.strip()
        if not out_line:
            raise GatewayError(502, "reasonix_acp_error", "engine shim produced no output")
        try:
            parsed = json.loads(out_line)
        except Exception as exc:
            raise GatewayError(502, "reasonix_acp_error", f"engine shim emitted non-JSON: {exc}")

        text = str(parsed.get("text") or "")
        su = parsed.get("usage") or {}
        in_tok = su.get("prompt_tokens")
        out_tok = su.get("completion_tokens")
        hit = su.get("prompt_cache_hit_tokens")
        miss = su.get("prompt_cache_miss_tokens")
        ratio = su.get("cache_hit_ratio")
        cost = parsed.get("cost_usd")
        # cache percent: prefer the shim's ratio (0..1 -> 0..100); fall back to
        # hit/(hit+miss) so the metric is non-null whenever token counts exist.
        cache = None
        if isinstance(ratio, (int, float)):
            cache = round(100.0 * float(ratio), 1)
        elif isinstance(hit, (int, float)) and isinstance(miss, (int, float)) and (hit + miss) > 0:
            cache = round(100.0 * float(hit) / float(hit + miss), 1)

        usage = {
            "input_tokens": int(in_tok) if isinstance(in_tok, (int, float))
            else estimate_tokens({"messages": [{"role": "user", "content": prompt}]}),
            "output_tokens": int(out_tok) if isinstance(out_tok, (int, float)) else max(1, len(text) // 4),
            # cache_pct is the ledger key (append_reasonix_cost reads reasonix_cache_pct
            # into a row's cache_pct); set both so cost/cache logging + realworld-bench
            # keep working.
            "cache_pct": cache,
            "reasonix_cost_usd": cost,
            "reasonix_cache_pct": cache,
            "reasonix_claude_equiv_usd": None,
        }
        # Prefix-cache diagnostics (opt-in via CLAUDE_REASONIX_GATEWAY_PREFIX_TRACE).
        # Unchanged from the acp path — hashes of the prompt prefix + this lane's
        # cache%, append-only JSONL, prompt text never logged.
        if os.getenv("CLAUDE_REASONIX_GATEWAY_PREFIX_TRACE", os.getenv("CLAUDE_CODEX_GATEWAY_PREFIX_TRACE", "")).lower() in {"1", "true", "yes", "on"}:
            try:
                import hashlib
                pfx4 = hashlib.sha1(prompt[:4096].encode("utf-8", "ignore")).hexdigest()[:12]
                pfx32 = hashlib.sha1(prompt[:32768].encode("utf-8", "ignore")).hexdigest()[:12]
                chunks = [prompt[i:i + 4096] for i in range(0, min(len(prompt), 131072), 4096)]
                chunk_hashes = [hashlib.sha1(c.encode("utf-8", "ignore")).hexdigest()[:10] for c in chunks]
                chunk_samples = [c[:80] for c in chunks]
                rec = {
                    "ts": _time.time(),
                    "prefix4k": pfx4,
                    "prefix32k": pfx32,
                    "prompt_len": len(prompt),
                    "cache_pct": cache,
                    "in_tok": usage["input_tokens"],
                    "chunk_hashes": chunk_hashes,
                    "chunk_samples": chunk_samples,
                }
                ledger_dir = Path(env_first(
                    "CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                    default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) / "runtime"
                ledger_dir.mkdir(parents=True, exist_ok=True)
                with open(ledger_dir / "prefix-trace.jsonl", "a", encoding="utf-8") as _pf:
                    _pf.write(json.dumps(rec) + "\n")
            except Exception:
                pass
        return text, usage

    # Prefix-prime gate: the first lane of a shared-prefix burst warms DeepSeek's
    # cache alone; later lanes wait (bounded) for that warm-up, then run together.
    is_primer, prime_gate = acquire_prime_role(prompt)
    if os.getenv("CLAUDE_REASONIX_GATEWAY_PREFIX_TRACE", os.getenv("CLAUDE_CODEX_GATEWAY_PREFIX_TRACE", "")).lower() in {"1", "true", "yes", "on"}:
        try:
            _pdir = Path(env_first("CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) / "runtime"
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
        wait_s = env_float("CLAUDE_REASONIX_GATEWAY_PRIME_WAIT_SECONDS", "CLAUDE_CODEX_GATEWAY_PRIME_WAIT_SECONDS", default=20.0)
        opened = prime_gate.wait(timeout=wait_s)
        # Post-open grace settle: DeepSeek persists the primed prefix in "seconds"
        # (per its cache docs), so let it finish writing before the waiters fire, or
        # they race the primer and miss the shared prefix. Measured: 1.5s let early
        # waiters race the primer (cache 65-81%); a few seconds lifts them to ~99%.
        # SKIP grace for serial-slot lanes: the per-key serial lock already forces
        # them to run strictly after the prior lane completes + its settle sleep, so
        # an extra grace here only adds dead wall-clock without improving the cache.
        if opened and serial_slot is False:
            grace = env_float("CLAUDE_REASONIX_GATEWAY_PRIME_GRACE_SECONDS", "CLAUDE_CODEX_GATEWAY_PRIME_GRACE_SECONDS", default=4.0)
            if grace > 0:
                _time.sleep(min(grace, 15.0))
    if serial_slot and os.getenv("CLAUDE_REASONIX_GATEWAY_PREFIX_TRACE", os.getenv("CLAUDE_CODEX_GATEWAY_PREFIX_TRACE", "")).lower() in {"1", "true", "yes", "on"}:
        try:
            _sdir = Path(env_first("CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) / "runtime"
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
    # a cold mid-burst lane. Env CLAUDE_REASONIX_GATEWAY_RETRY_EMPTY: "burst" (default) =
    # isolated-only; "1"/"all" = always (legacy, re-introduces burst variance);
    # "0"/off = never.
    _re = os.getenv("CLAUDE_REASONIX_GATEWAY_RETRY_EMPTY", os.getenv("CLAUDE_CODEX_GATEWAY_RETRY_EMPTY", "burst")).lower()
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
            # Truncation recovery (Lever A): an A-capped read lane can spend its small
            # cap on tool-calls/reasoning/outline and get truncated before emitting the
            # answer -> empty text. A SAME-cap retry won't help (the budget is the
            # cause); re-run at a PROGRESSIVELY higher cap until the model can finish.
            # One 2x bump recovers most lanes, but the heaviest (a "walk through every
            # function" on a 134KB file) need more; escalate 2x, 4x, ... up to a final
            # UNCAPPED attempt (measured: no-cap = 0% hollow). Runs even mid-burst
            # (force) because a lost summary is worse than a few extra-budget lanes.
            # Only when the lane was actually capped (max_output_tokens set).
            if (not str(result[0]).strip()) and retry_empty_force and max_output_tokens is not None:
                _max_escalations = env_int(
                    "CLAUDE_REASONIX_GATEWAY_READ_RETRY_MAX_ESCALATIONS",
                    "CLAUDE_CODEX_GATEWAY_READ_RETRY_MAX_ESCALATIONS", default=3)
                _cap = max_output_tokens
                _recovered = False
                for _esc in range(1, _max_escalations + 1):
                    _bigger = retry_cap_for_empty(_cap, True, True)
                    # final escalation drops the cap entirely (the proven 0% case);
                    # 0 is the "explicitly uncapped" sentinel for _attempt.
                    _override = _bigger if _esc < _max_escalations else 0
                    gateway_trace("reasonix_acp_uncap_retry", model=model,
                                  attempt=attempt, escalation=_esc, new_cap=_override)
                    _r2 = _attempt(cap_override=_override)
                    if str(_r2[0]).strip():
                        return _r2
                    last_result = _r2
                    if _override == 0:
                        break  # already uncapped; nothing higher to try
                    _cap = _bigger
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
            settle = env_float("CLAUDE_REASONIX_GATEWAY_PRIME_SERIAL_SETTLE_SECONDS", "CLAUDE_CODEX_GATEWAY_PRIME_SERIAL_SETTLE_SECONDS", default=4.0)
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
        record_keepalive_prefix(prompt)
        lane_type = classify_lane_type(payload.get("tools"), lane_task_text(normalized))
        # Lever F HARD layer (default off): cap output by lane-type budget.
        # Lever A HARD layer (default off): for read lanes, read_summary_budget()
        # returns 512 when READ_SUMMARY is on.  Both levers agree on 512 for read
        # lanes; pick the tighter (smallest) non-None cap.
        _f_cap = output_discipline_budget(lane_type)
        _a_cap = read_summary_budget() if lane_type == "read" else None
        _caps = [c for c in (_f_cap, _a_cap) if c is not None]
        _max_out = min(_caps) if _caps else None
        # Lever G (default off): reject lanes whose file scope is too broad.
        _rej = overscope_rejection(lane_task_text(normalized),
                                   env_first("CLAUDE_REASONIX_GATEWAY_CWD",
                                             "CLAUDE_CODEX_GATEWAY_CODEX_CWD",
                                             default=os.getcwd()))
        if _rej is not None:
            return {
                "id": f"chatcmpl_{uuid4().hex}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": requested_model,
                "choices": [{"index": 0,
                              "message": {"role": "assistant", "content": _rej},
                              "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        # Lever A truncation recovery (see /v1/messages path): retry an empty A-capped
        # read lane once at a higher cap. Same flag/gate.
        _retry_hollow = (_a_cap is not None) and env_truthy(
            "CLAUDE_REASONIX_GATEWAY_READ_RETRY_HOLLOW",
            "CLAUDE_CODEX_GATEWAY_READ_RETRY_HOLLOW", default="1")
        # C3: symmetric with /v1/messages path (flag off -> byte-identical).
        _harness = None
        if _lane_harness_on():
            _at = lane_acceptance_test(normalized)
            if _at:
                _harness = {
                    "acceptanceTest": _at,
                    "budgetUsd": env_float("CLAUDE_REASONIX_GATEWAY_LANE_BUDGET_USD",
                                          "CLAUDE_CODEX_GATEWAY_LANE_BUDGET_USD", default=0.05),
                    "harnessMaxAttempts": env_int("CLAUDE_REASONIX_GATEWAY_LANE_MAX_ATTEMPTS",
                                                  "CLAUDE_CODEX_GATEWAY_LANE_MAX_ATTEMPTS", default=4),
                }
        text, usage = run_reasonix_acp(
            prompt, config, max_output_tokens=_max_out,
            retry_empty_force=_retry_hollow, harness=_harness)
        # C3: fold harness reply BEFORE populate_read_cache / ledger.
        _hp = parse_harness_result(text)
        if _hp is not None:
            text = harness_lane_reply(_hp)
        # Lever C (default off): populate the shared read-cache from this lane's
        # summary (see /v1/messages path). No-op when off; best-effort.
        populate_read_cache(prompt, text)
        gateway_trace("reasonix_acp_openai_response", model=requested_model,
                      cost=usage.get("reasonix_cost_usd"), cache=usage.get("reasonix_cache_pct"))
        ledger = env_first(
            "CLAUDE_REASONIX_REASONIX_COST_LEDGER", "CLAUDE_CODEX_REASONIX_COST_LEDGER",
            default=str(Path(env_first("CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                                       default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) / "runtime" / "reasonix-cost.jsonl"),
        )
        append_reasonix_cost(
            ledger, usage,
            cwd=env_first("CLAUDE_REASONIX_GATEWAY_CWD", "CLAUDE_CODEX_GATEWAY_CODEX_CWD", default=os.getcwd()),
            model=str(config.get("target_model") or ""),
            claude_equiv=usage.get("reasonix_claude_equiv_usd"),
            lane_type=lane_type,
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
        if os.getenv("CLAUDE_REASONIX_GATEWAY_STRUCTURED_DEBUG", os.getenv("CLAUDE_CODEX_GATEWAY_STRUCTURED_DEBUG", "")).lower() in {"1", "true", "yes", "on"}:
            try:
                _dbg_dir = Path(env_first("CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                    default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) / "runtime"
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
            if tool_input is not None:
                clear_lane_count(prompt)  # real output -> family not looping, reset count
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


