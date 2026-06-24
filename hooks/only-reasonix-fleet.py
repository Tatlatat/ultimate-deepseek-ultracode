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
    return os.getenv("CLAUDE_REASONIX_NATIVE_SUBAGENTS", os.getenv("CLAUDE_CODEX_NATIVE_SUBAGENTS", "0")).lower() in {"1", "true", "yes", "on"}


def flavor() -> str:
    return os.getenv("CLAUDE_REASONIX_FLAVOR", os.getenv("CLAUDE_CODEX_FLAVOR", "reasonix")).strip().lower()


def payload_mentions_native_agent(payload) -> bool:
    # reasonix-* are the current agentType names; codex-*/deepseek-* are accepted for
    # back-compat with an in-flight session whose launcher predates the rename.
    for value in iter_strings(payload):
        lowered = value.lower()
        if lowered.startswith(("reasonix-", "codex-", "deepseek-")):
            return True
        if "agent(reasonix-" in lowered or "agent(codex-" in lowered or "agent(deepseek-" in lowered:
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

    # Block the native Agent tool in BOTH flavors and push to the reasonix_fleet MCP.
    # The MCP is now flavor-aware: in a reasonix session it runs reasonix acp (not
    # codex exec), so the subagent runs on Reasonix. We must NOT let the native
    # Agent tool through — it goes through the harness dispatch/classifier which
    # hangs (0 tokens). The MCP is the working escape hatch for both engines.
    shown = tool_name or "<unknown>"
    worker = "run_reasonix_worker" if flavor() != "reasonix" else "run_reasonix_worker (runs Reasonix in this session)"
    print(
        "Codex Fleet subagent policy blocked Claude native subagent tool "
        f"{shown}. Use mcp__reasonix_fleet__{worker} or "
        "mcp__reasonix_fleet__run_reasonix_fleet instead.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
