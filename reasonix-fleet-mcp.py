#!/usr/bin/env python3
import asyncio
from functools import partial
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any


SERVER_NAME = "reasonix-fleet"
SERVER_VERSION = "1.0.0"


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


DEFAULT_CONCURRENCY = env_int("REASONIX_FLEET_DEFAULT_CONCURRENCY", 16)
DEFAULT_TIMEOUT = env_int("REASONIX_FLEET_TIMEOUT_SECONDS", 1800)
DEFAULT_MAX_OUTPUT = env_int("REASONIX_FLEET_MAX_OUTPUT_CHARS", 8000)
REASONIX_BIN = os.getenv("REASONIX_BIN", os.getenv("CODEX_BIN", "reasonix"))
# Default the log dir next to this MCP file (the install dir), so the repo carries
# no machine path; the launcher overrides it via REASONIX_FLEET_LOG_DIR.
LOG_DIR = Path(os.getenv(
    "REASONIX_FLEET_LOG_DIR",
    str(Path(__file__).resolve().parent / "runtime" / "logs"),
))


def fleet_flavor() -> str:
    return os.getenv("CLAUDE_REASONIX_FLAVOR", os.getenv("CLAUDE_CODEX_FLAVOR", "reasonix")).strip().lower()


_RX_GATEWAY = None


def _reasonix_gateway_module():
    """Lazily import the gateway module (same repo) so the MCP worker reuses the
    exact, already-tested run_reasonix_acp + append_reasonix_cost. Cached;
    returns None if it can't be loaded."""
    global _RX_GATEWAY
    if _RX_GATEWAY is not None:
        return _RX_GATEWAY if _RX_GATEWAY is not False else None
    try:
        import importlib.util as _ilu
        gw_path = Path(__file__).resolve().parent / "reasonix-native-gateway.py"
        spec = _ilu.spec_from_file_location("_rx_gateway", gw_path)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _RX_GATEWAY = mod
    except Exception:
        _RX_GATEWAY = False
        return None
    return _RX_GATEWAY


def _reasonix_acp_fn():
    mod = _reasonix_gateway_module()
    return getattr(mod, "run_reasonix_acp", None) if mod is not None else None


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    head = max(0, limit // 2)
    tail = max(0, limit - head)
    return text[:head] + "\n...[truncated]...\n" + text[-tail:], True


def task_value(task: dict[str, Any], key: str, env_name: str, default: str) -> str:
    value = task.get(key)
    if value is None or value == "":
        value = os.getenv(env_name, default)
    return str(value)


async def run_one_task(task: dict[str, Any], index: int, batch_id: str, max_output_chars: int) -> dict[str, Any]:
    title = str(task.get("title") or task.get("name") or f"task-{index + 1}")
    started = time.monotonic()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe_title = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in title.lower())[:60] or "task"
    log_prefix = LOG_DIR / f"{batch_id}-{index + 1:04d}-{safe_title}"
    stdout_path = str(log_prefix) + ".stdout.txt"
    stderr_path = str(log_prefix) + ".stderr.txt"

    # Always dispatch through reasonix acp via the gateway's tested ACP driver.
    rx = _reasonix_acp_fn()
    if rx is None:
        return {"index": index, "title": title, "ok": False,
                "error": "reasonix acp unavailable: gateway module not importable",
                "duration_ms": int((time.monotonic() - started) * 1000)}
    prompt = str(task.get("prompt") or task.get("task") or "")
    if not prompt.strip():
        return {"index": index, "title": title, "ok": False,
                "error": "task prompt is required",
                "duration_ms": int((time.monotonic() - started) * 1000)}
    cwd = task.get("cwd")
    cwd_text = str(cwd) if cwd else os.getcwd()
    model = task_value(task, "model", "CLAUDE_REASONIX_REASONIX_MODEL", "deepseek-v4-flash")
    # The gateway's run_reasonix_acp now spawns the in-process fork-engine shim
    # (`node engine/run-lane.mjs`), not upstream `reasonix acp`, so only the model
    # is dispatch-relevant; the legacy reasonix_bin config key is no longer read.
    config = {"target_model": model}
    # Harness: engage when the gateway flag is on AND the prompt has an ACCEPTANCE_TEST line.
    # When the flag is unset, _harness stays None and partial(rx, prompt, config, harness=None)
    # is behaviorally identical to rx(prompt, config) — byte-inert.
    gw = _reasonix_gateway_module()
    _harness = None
    if gw is not None and getattr(gw, "_lane_harness_on", None) and gw._lane_harness_on():
        _at = gw.lane_acceptance_test([{"role": "user", "content": prompt}])
        if _at:
            _harness = {
                "acceptanceTest": _at,
                "budgetUsd": gw.env_float("CLAUDE_REASONIX_GATEWAY_LANE_BUDGET_USD",
                                          "CLAUDE_CODEX_GATEWAY_LANE_BUDGET_USD", default=0.05),
                "harnessMaxAttempts": gw.env_int("CLAUDE_REASONIX_GATEWAY_LANE_MAX_ATTEMPTS",
                                                 "CLAUDE_CODEX_GATEWAY_LANE_MAX_ATTEMPTS", default=4),
            }
    # run_reasonix_acp reads cwd from CLAUDE_REASONIX_GATEWAY_CWD.
    prev_cwd = os.environ.get("CLAUDE_REASONIX_GATEWAY_CWD", os.environ.get("CLAUDE_CODEX_GATEWAY_CODEX_CWD"))
    os.environ["CLAUDE_REASONIX_GATEWAY_CWD"] = cwd_text
    try:
        loop = asyncio.get_running_loop()
        text, usage = await loop.run_in_executor(None, partial(rx, prompt, config, harness=_harness))
    except Exception as exc:
        return {"index": index, "title": title, "ok": False,
                "error": f"reasonix acp failed: {exc}",
                "duration_ms": int((time.monotonic() - started) * 1000)}
    finally:
        if prev_cwd is None:
            os.environ.pop("CLAUDE_REASONIX_GATEWAY_CWD", None)
        else:
            os.environ["CLAUDE_REASONIX_GATEWAY_CWD"] = prev_cwd
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    Path(stdout_path).write_text(text or "", encoding="utf-8")
    # Record the lane in the per-session cost ledger so `claude-reasonix cost`
    # counts MCP-dispatched subagents too (same ledger as gateway lanes).
    try:
        gw = _reasonix_gateway_module()
        ledger = os.getenv("CLAUDE_REASONIX_REASONIX_COST_LEDGER", os.getenv("CLAUDE_CODEX_REASONIX_COST_LEDGER",
                           str(LOG_DIR.parent / "reasonix-cost.jsonl")))
        if gw is not None and hasattr(gw, "append_reasonix_cost"):
            gw.append_reasonix_cost(ledger, usage, cwd=cwd_text, model=model,
                                    claude_equiv=usage.get("reasonix_claude_equiv_usd"))
    except Exception:
        pass
    preview, truncated = truncate(text or "", max_output_chars)
    return {
        "index": index, "title": title, "ok": True, "exit_code": 0,
        "engine": "reasonix", "model": model, "cwd": cwd_text,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "stdout": preview, "stdout_truncated": truncated,
        "stdout_log": stdout_path,
        "reasonix_cost_usd": usage.get("reasonix_cost_usd"),
        "reasonix_cache_pct": usage.get("reasonix_cache_pct"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


async def run_fleet(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_tasks = arguments.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("tasks must be a non-empty array")

    tasks: list[dict[str, Any]] = []
    for item in raw_tasks:
        if isinstance(item, str):
            tasks.append({"prompt": item})
        elif isinstance(item, dict):
            tasks.append(item)
        else:
            raise ValueError("each task must be an object or string")

    concurrency = int(arguments.get("concurrency") or DEFAULT_CONCURRENCY)
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")

    max_output_chars = int(arguments.get("max_output_chars") or DEFAULT_MAX_OUTPUT)
    batch_id = str(arguments.get("batch_id") or uuid.uuid4().hex[:12])
    semaphore = asyncio.Semaphore(concurrency)
    started = time.monotonic()

    async def guarded(task: dict[str, Any], index: int) -> dict[str, Any]:
        async with semaphore:
            return await run_one_task(task, index, batch_id, max_output_chars)

    results = await asyncio.gather(*(guarded(task, idx) for idx, task in enumerate(tasks)))
    ok_count = sum(1 for result in results if result.get("ok"))

    return {
        "batch_id": batch_id,
        "total_tasks": len(tasks),
        "concurrency": concurrency,
        "ok_tasks": ok_count,
        "failed_tasks": len(tasks) - ok_count,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "results": results,
    }


async def run_worker(arguments: dict[str, Any]) -> dict[str, Any]:
    task = dict(arguments)
    if "prompt" not in task and "task" not in task:
        raise ValueError("prompt is required")
    return await run_one_task(task, 0, uuid.uuid4().hex[:12], int(task.get("max_output_chars") or DEFAULT_MAX_OUTPUT))


def tool_definitions() -> list[dict[str, Any]]:
    task_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "prompt": {"type": "string"},
            "cwd": {"type": "string"},
            "model": {"type": "string"},
            "reasoning_effort": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1},
            "skip_git_repo_check": {"type": "boolean"},
        },
        "required": ["prompt"],
        "additionalProperties": True,
    }
    return [
        {
            "name": "run_reasonix_worker",
            "description": "Run one Reasonix subagent (reasonix acp). The Reasonix process exits when the task is complete.",
            "inputSchema": task_schema,
        },
        {
            "name": "run_reasonix_fleet",
            "description": "Run any number of Reasonix CLI subagent tasks through a dynamic queue. Use this for dynamic workflows and large batches.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tasks": {"type": "array", "items": task_schema, "minItems": 1},
                    "concurrency": {"type": "integer", "minimum": 1},
                    "max_output_chars": {"type": "integer", "minimum": 0},
                    "batch_id": {"type": "string"},
                },
                "required": ["tasks"],
                "additionalProperties": False,
            },
        },
        {
            "name": "fleet_status",
            "description": "Show Reasonix Fleet runtime defaults.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "run_reasonix_fleet":
        payload = await run_fleet(arguments)
    elif name == "run_reasonix_worker":
        payload = await run_worker(arguments)
    elif name == "fleet_status":
        payload = {
            "reasonix_bin": REASONIX_BIN,
            "default_concurrency": DEFAULT_CONCURRENCY,
            "default_timeout_seconds": DEFAULT_TIMEOUT,
            "default_max_output_chars": DEFAULT_MAX_OUTPUT,
            "log_dir": str(LOG_DIR),
            "model": os.getenv("REASONIX_FLEET_MODEL", "deepseek-v4-flash"),
            "reasoning_effort": os.getenv("REASONIX_FLEET_REASONING", "xhigh"),
        }
    else:
        raise ValueError(f"unknown tool: {name}")
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=True, indent=2)}],
        "isError": False,
    }


def response(message_id: Any, result: Any = None, error: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return payload


async def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return response(
            message_id,
            {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return response(message_id, {"tools": tool_definitions()})
    if method == "tools/call":
        try:
            result = await call_tool(str(params.get("name")), params.get("arguments") or {})
            return response(message_id, result)
        except Exception as exc:
            return response(
                message_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )

    return response(message_id, error={"code": -32601, "message": f"method not found: {method}"})


async def main() -> None:
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            return
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stdout.write(json.dumps(response(None, error={"code": -32700, "message": str(exc)})) + "\n")
            sys.stdout.flush()
            continue
        result = await handle(message)
        if result is not None:
            sys.stdout.write(json.dumps(result) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
