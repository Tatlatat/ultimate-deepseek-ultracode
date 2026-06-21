#!/usr/bin/env python3
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any


SERVER_NAME = "codex-fleet"
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


DEFAULT_CONCURRENCY = env_int("CODEX_FLEET_DEFAULT_CONCURRENCY", 16)
DEFAULT_TIMEOUT = env_int("CODEX_FLEET_TIMEOUT_SECONDS", 1800)
DEFAULT_MAX_OUTPUT = env_int("CODEX_FLEET_MAX_OUTPUT_CHARS", 8000)
CODEX_BIN = os.getenv("CODEX_BIN", "codex")
LOG_DIR = Path(os.getenv("CODEX_FLEET_LOG_DIR", "/Users/tatlatat/.claude/codex-fleet/runtime/logs"))


def fleet_flavor() -> str:
    return os.getenv("CLAUDE_CODEX_FLAVOR", "codex").strip().lower()


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
        gw_path = Path(__file__).resolve().parent / "codex-native-gateway.py"
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


def default_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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


def build_codex_command(task: dict[str, Any]) -> tuple[list[str], str | None, str]:
    prompt = str(task.get("prompt") or task.get("task") or "")
    if not prompt.strip():
        raise ValueError("task prompt is required")

    cwd = task.get("cwd")
    cwd_text = str(cwd) if cwd else None

    model = task_value(task, "model", "CODEX_FLEET_MODEL", "gpt-5.4")
    reasoning = task_value(task, "reasoning_effort", "CODEX_FLEET_REASONING", "xhigh")
    service_tier = task_value(task, "service_tier", "CODEX_FLEET_SERVICE_TIER", "fast")
    web_search = task_value(task, "web_search", "CODEX_FLEET_WEB_SEARCH", "live")
    sandbox = task_value(task, "sandbox", "CODEX_FLEET_SANDBOX", "workspace-write")
    approval = task_value(task, "approval_policy", "CODEX_FLEET_APPROVAL", "never")

    skip_git = bool(task.get("skip_git_repo_check", default_bool("CODEX_FLEET_SKIP_GIT_REPO_CHECK", True)))

    command = [
        CODEX_BIN,
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
    ]

    if cwd_text:
        command.extend(["-C", cwd_text])
    if skip_git:
        command.append("--skip-git-repo-check")

    command.append("-")
    return command, cwd_text, prompt


async def run_one_task(task: dict[str, Any], index: int, batch_id: str, max_output_chars: int) -> dict[str, Any]:
    title = str(task.get("title") or task.get("name") or f"task-{index + 1}")
    started = time.monotonic()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe_title = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in title.lower())[:60] or "task"
    log_prefix = LOG_DIR / f"{batch_id}-{index + 1:04d}-{safe_title}"
    stdout_path = str(log_prefix) + ".stdout.txt"
    stderr_path = str(log_prefix) + ".stderr.txt"

    # Reasonix flavor: run the worker through reasonix acp (NOT codex exec), via the
    # gateway's tested ACP driver. This is what makes single subagents in a
    # claude-reasonix session actually run on Reasonix instead of Codex.
    if fleet_flavor() == "reasonix":
        rx = _reasonix_acp_fn()
        if rx is not None:
            prompt = str(task.get("prompt") or task.get("task") or "")
            if not prompt.strip():
                return {"index": index, "title": title, "ok": False,
                        "error": "task prompt is required",
                        "duration_ms": int((time.monotonic() - started) * 1000)}
            cwd = task.get("cwd")
            cwd_text = str(cwd) if cwd else os.getcwd()
            model = task_value(task, "model", "CLAUDE_CODEX_REASONIX_MODEL", "deepseek-v4-flash")
            config = {"reasonix_bin": os.getenv("REASONIX_BIN", "reasonix"),
                      "target_model": model}
            # run_reasonix_acp reads cwd from CLAUDE_CODEX_GATEWAY_CODEX_CWD.
            prev_cwd = os.environ.get("CLAUDE_CODEX_GATEWAY_CODEX_CWD")
            os.environ["CLAUDE_CODEX_GATEWAY_CODEX_CWD"] = cwd_text
            try:
                loop = asyncio.get_running_loop()
                text, usage = await loop.run_in_executor(None, rx, prompt, config)
            except Exception as exc:
                return {"index": index, "title": title, "ok": False,
                        "error": f"reasonix acp failed: {exc}",
                        "duration_ms": int((time.monotonic() - started) * 1000)}
            finally:
                if prev_cwd is None:
                    os.environ.pop("CLAUDE_CODEX_GATEWAY_CODEX_CWD", None)
                else:
                    os.environ["CLAUDE_CODEX_GATEWAY_CODEX_CWD"] = prev_cwd
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            Path(stdout_path).write_text(text or "", encoding="utf-8")
            # Record the lane in the per-session cost ledger so `claude-reasonix cost`
            # counts MCP-dispatched subagents too (same ledger as gateway lanes).
            try:
                gw = _reasonix_gateway_module()
                ledger = os.getenv("CLAUDE_CODEX_REASONIX_COST_LEDGER",
                                   str(LOG_DIR.parent / "reasonix-cost.jsonl"))
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

    try:
        command, cwd, prompt = build_codex_command(task)
    except Exception as exc:
        return {
            "index": index,
            "title": title,
            "ok": False,
            "error": str(exc),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    timeout = int(task.get("timeout_seconds") or DEFAULT_TIMEOUT)

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(prompt.encode("utf-8")),
            timeout=timeout,
        )
        exit_code = process.returncode
    except asyncio.TimeoutError:
        try:
            process.kill()
        except Exception:
            pass
        stdout_bytes, stderr_bytes = b"", f"Timed out after {timeout} seconds".encode("utf-8")
        exit_code = 124
    except Exception as exc:
        stdout_bytes, stderr_bytes = b"", str(exc).encode("utf-8")
        exit_code = 1

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    Path(stdout_path).write_text(stdout, encoding="utf-8")
    Path(stderr_path).write_text(stderr, encoding="utf-8")
    stdout_preview, stdout_truncated = truncate(stdout, max_output_chars)
    stderr_preview, stderr_truncated = truncate(stderr, max_output_chars)

    return {
        "index": index,
        "title": title,
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "cwd": cwd,
        "command": command[:2] + ["..."],
        "stdout": stdout_preview,
        "stderr": stderr_preview,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "stdout_log": stdout_path,
        "stderr_log": stderr_path,
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
            "service_tier": {"type": "string"},
            "web_search": {"type": "string", "enum": ["cached", "live", "disabled"]},
            "sandbox": {"type": "string", "enum": ["read-only", "workspace-write", "danger-full-access"]},
            "approval_policy": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1},
            "skip_git_repo_check": {"type": "boolean"},
        },
        "required": ["prompt"],
        "additionalProperties": True,
    }
    return [
        {
            "name": "run_codex_worker",
            "description": "Run one Codex CLI subagent with codex exec. The Codex process exits when the task is complete.",
            "inputSchema": task_schema,
        },
        {
            "name": "run_codex_fleet",
            "description": "Run any number of Codex CLI subagent tasks through a dynamic queue. Use this for dynamic workflows and large batches.",
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
            "description": "Show Codex Fleet runtime defaults.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "run_codex_fleet":
        payload = await run_fleet(arguments)
    elif name == "run_codex_worker":
        payload = await run_worker(arguments)
    elif name == "fleet_status":
        payload = {
            "codex_bin": CODEX_BIN,
            "default_concurrency": DEFAULT_CONCURRENCY,
            "default_timeout_seconds": DEFAULT_TIMEOUT,
            "default_max_output_chars": DEFAULT_MAX_OUTPUT,
            "log_dir": str(LOG_DIR),
            "model": os.getenv("CODEX_FLEET_MODEL", "gpt-5.4"),
            "reasoning_effort": os.getenv("CODEX_FLEET_REASONING", "xhigh"),
            "service_tier": os.getenv("CODEX_FLEET_SERVICE_TIER", "fast"),
            "web_search": os.getenv("CODEX_FLEET_WEB_SEARCH", "live"),
            "sandbox": os.getenv("CODEX_FLEET_SANDBOX", "workspace-write"),
            "approval_policy": os.getenv("CODEX_FLEET_APPROVAL", "never"),
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
