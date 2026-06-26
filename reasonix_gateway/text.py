import json
from typing import Any

from .env import JSON


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


def lane_task_text(messages: Any) -> str:
    """The lane's RAW task text — the user/system message content BEFORE the gateway
    appends any directive (structured-output instruction, F's discipline directive,
    A's summary instruction, the cache block). Classify on THIS, never on the fully
    assembled prompt: every injected directive carries edit/read keywords (e.g. the
    structured instruction says 'Do NOT write sentences like…'), which would flip a
    read lane to 'edit' and silently disable the per-type output cap. Measured: with
    the cap keyed off the assembled prompt, 0 read/review lanes were ever classified
    as read — all 150 became 'edit'."""
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") in {"user", "system"}:
            parts.append(text_from_content(m.get("content")))
    return "\n\n".join(p for p in parts if p)
