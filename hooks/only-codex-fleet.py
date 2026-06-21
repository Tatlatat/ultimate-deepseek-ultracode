#!/usr/bin/env python3
import json
import os
import re
import sys


def iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)


def native_subagents_enabled() -> bool:
    return os.getenv("CLAUDE_CODEX_NATIVE_SUBAGENTS", "0").lower() in {"1", "true", "yes", "on"}


def flavor() -> str:
    return os.getenv("CLAUDE_CODEX_FLAVOR", "codex").strip().lower()


def payload_mentions_native_agent(payload) -> bool:
    for value in iter_strings(payload):
        lowered = value.lower()
        if lowered.startswith(("codex-", "deepseek-")):
            return True
        if "agent(codex-" in lowered or "agent(deepseek-" in lowered:
            return True
    return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        print(f"Codex fleet guard blocked tool call: invalid hook JSON ({exc})", file=sys.stderr)
        return 2

    tool_name = str(payload.get("tool_name") or "")
    blocked = {
        "Agent",
        "Task",
        "Subagent",
        "SubAgent",
        "SpawnAgent",
        "AgentSpawn",
        "TaskAgent",
    }
    if tool_name not in blocked:
        return 0

    if native_subagents_enabled() and payload_mentions_native_agent(payload):
        return 0

    # Reasonix flavor: do NOT push subagents to the codex_fleet MCP (which runs
    # `codex exec` = Codex, not Reasonix). Let the native Agent tool run so the
    # lane routes through the CCR proxy → gateway → reasonix acp, keeping ALL
    # agents on Reasonix as the session intends. The codex-fleet MCP has no
    # reasonix backend, so forcing it here would silently run Codex instead.
    if flavor() == "reasonix":
        return 0

    shown = tool_name or "<unknown>"
    print(
        "Codex Fleet subagent policy blocked Claude native subagent tool "
        f"{shown}. Use mcp__codex_fleet__run_codex_worker or "
        "mcp__codex_fleet__run_codex_fleet instead.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
