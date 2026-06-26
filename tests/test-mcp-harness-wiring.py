#!/usr/bin/env python3
"""e2e test: the harness is wired into the MCP worker (run_one_task).

Gap 1: when CLAUDE_REASONIX_GATEWAY_LANE_HARNESS=1 and the prompt has an
ACCEPTANCE_TEST line, run_one_task builds and forwards a harness dict through
run_reasonix_acp → node engine/run-lane.mjs → runHarness.  The shim returns a
`__HARNESS__:<status>:<attempts>:<lesson>` summary instead of a plain reply.

Gap 2 (covered by Gap 1): a slow lane is now bounded (maxAttempts/budgetUsd) and
returns NORMALLY — its cost lands in the cost ledger — rather than hanging until
the 600 s gateway timeout fires.

Four cases:
 1. flag ON + ACCEPTANCE_TEST: true  → stdout starts with __HARNESS__:pass:
 2. flag OFF (unset)              → plain mock reply, NO __HARNESS__ prefix
 3. flag ON  + NO acceptance line → plain mock reply, NO __HARNESS__ prefix
 4. flag ON  + ACCEPTANCE_TEST: false (always fails) → __HARNESS__: non-pass
    AND the cost ledger gets >= 1 row (bounded-failure cost logging works)

All cases use REASONIX_ENGINE_MOCK=1 so no DeepSeek is contacted.  The harness
path (cases 1 + 4) uses `true`/`false` shell builtins as acceptance tests —
fast, deterministic, no codebase needed.

The test imports the REAL MCP module and calls run_one_task via asyncio.run —
e2e over the real MCP → gateway → shim subprocess path.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
DIST = str(ROOT / "vendor" / "reasonix-engine" / "dist" / "index.js")

# ── load the MCP module fresh for each test (exec_module + fresh cache) ──────
def _load_mcp():
    spec = importlib.util.spec_from_file_location(
        "_mcp_harness_test", ROOT / "reasonix-fleet-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Also reload the cached gateway module so env changes are picked up.
    mod._RX_GATEWAY = None  # noqa: SLF001 — force lazy-reload
    return mod


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


# ── shared env base ──────────────────────────────────────────────────────────
BASE_ENV = {
    "CLAUDE_REASONIX_FLAVOR": "reasonix",
    "REASONIX_ENGINE_MOCK": "1",
    "REASONIX_ENGINE_MOCK_COST": "0.000111",
    "REASONIX_ENGINE_MOCK_PROMPT_TOKENS": "10",
    "REASONIX_ENGINE_MOCK_COMPLETION_TOKENS": "3",
    "REASONIX_ENGINE_MOCK_CACHE_HIT_TOKENS": "8",
    "REASONIX_ENGINE_MOCK_CACHE_MISS_TOKENS": "2",
    "REASONIX_ENGINE_DIST": DIST,
}

# Keys we might set (need to be restored after each test).
VOLATILE = [
    "CLAUDE_REASONIX_FLAVOR",
    "REASONIX_ENGINE_MOCK",
    "REASONIX_ENGINE_MOCK_TEXT",
    "REASONIX_ENGINE_MOCK_COST",
    "REASONIX_ENGINE_MOCK_PROMPT_TOKENS",
    "REASONIX_ENGINE_MOCK_COMPLETION_TOKENS",
    "REASONIX_ENGINE_MOCK_CACHE_HIT_TOKENS",
    "REASONIX_ENGINE_MOCK_CACHE_MISS_TOKENS",
    "REASONIX_ENGINE_DIST",
    "CLAUDE_REASONIX_GATEWAY_LANE_HARNESS",
    "CLAUDE_CODEX_GATEWAY_LANE_HARNESS",
    "CLAUDE_REASONIX_REASONIX_COST_LEDGER",
    "CLAUDE_CODEX_REASONIX_COST_LEDGER",
    "REASONIX_FLEET_LOG_DIR",
]


class _EnvCtx:
    """Context manager: set env vars, restore on exit."""
    def __init__(self, **kwargs):
        self._new = kwargs
        self._saved: dict[str, str | None] = {}

    def __enter__(self):
        for k, v in self._new.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run_task(mcp, prompt: str, cwd: str) -> dict:
    task = {"title": "harness-test", "prompt": prompt, "cwd": cwd}
    return asyncio.run(mcp.run_one_task(task, 0, "batch-harnesstest", 20000))


# ── Case 1: flag ON + ACCEPTANCE_TEST: true → __HARNESS__:pass: ──────────────
def test_case1_harness_on_passing():
    """Harness engaged through the full MCP → gateway → shim chain; test passes."""
    prompt = "Do something.\nACCEPTANCE_TEST: true\nEnd."
    with tempfile.TemporaryDirectory() as cwd:
        env = {
            **BASE_ENV,
            "CLAUDE_REASONIX_GATEWAY_LANE_HARNESS": "1",
            "REASONIX_FLEET_LOG_DIR": str(Path(cwd) / "logs"),
        }
        with _EnvCtx(**env):
            mcp = _load_mcp()
            result = _run_task(mcp, prompt, cwd)

    expect(result.get("ok") is True, f"case1: task must succeed, got: {result}")
    stdout = result.get("stdout") or ""
    expect(
        stdout.startswith("__HARNESS__:pass:"),
        f"case1: stdout must start with __HARNESS__:pass: — got: {stdout!r}",
    )
    print("  ok   case1: flag ON + ACCEPTANCE_TEST:true → __HARNESS__:pass:")


# ── Case 2: flag OFF → plain mock reply, NO __HARNESS__ ─────────────────────
def test_case2_flag_off_byte_inert():
    """When flag is unset, run_one_task produces the plain mock reply (byte-inert)."""
    prompt = "Do something.\nACCEPTANCE_TEST: true\nEnd."
    mock_text = "MOCK_PLAIN_REPLY_NO_HARNESS"
    with tempfile.TemporaryDirectory() as cwd:
        env = {
            **BASE_ENV,
            "REASONIX_ENGINE_MOCK_TEXT": mock_text,
            # NO CLAUDE_REASONIX_GATEWAY_LANE_HARNESS set
            "CLAUDE_REASONIX_GATEWAY_LANE_HARNESS": None,  # explicit unset
            "CLAUDE_CODEX_GATEWAY_LANE_HARNESS": None,
            "REASONIX_FLEET_LOG_DIR": str(Path(cwd) / "logs"),
        }
        with _EnvCtx(**env):
            mcp = _load_mcp()
            result = _run_task(mcp, prompt, cwd)

    expect(result.get("ok") is True, f"case2: task must succeed, got: {result}")
    stdout = result.get("stdout") or ""
    expect(
        mock_text in stdout,
        f"case2: stdout must contain mock text ({mock_text!r}), got: {stdout!r}",
    )
    expect(
        not stdout.startswith("__HARNESS__:"),
        f"case2: stdout must NOT start with __HARNESS__ when flag is off, got: {stdout!r}",
    )
    print("  ok   case2: flag OFF → plain mock reply, no __HARNESS__ (byte-inert)")


# ── Case 3: flag ON + NO acceptance line → plain mock reply ─────────────────
def test_case3_flag_on_no_acceptance_line():
    """Harness flag on but no ACCEPTANCE_TEST in prompt → harness not engaged."""
    prompt = "Do something without an acceptance test line."
    mock_text = "MOCK_NO_ACCEPTANCE"
    with tempfile.TemporaryDirectory() as cwd:
        env = {
            **BASE_ENV,
            "REASONIX_ENGINE_MOCK_TEXT": mock_text,
            "CLAUDE_REASONIX_GATEWAY_LANE_HARNESS": "1",
            "REASONIX_FLEET_LOG_DIR": str(Path(cwd) / "logs"),
        }
        with _EnvCtx(**env):
            mcp = _load_mcp()
            result = _run_task(mcp, prompt, cwd)

    expect(result.get("ok") is True, f"case3: task must succeed, got: {result}")
    stdout = result.get("stdout") or ""
    expect(
        mock_text in stdout,
        f"case3: stdout must contain mock text ({mock_text!r}), got: {stdout!r}",
    )
    expect(
        not stdout.startswith("__HARNESS__:"),
        f"case3: stdout must NOT start with __HARNESS__ when no acceptance line, got: {stdout!r}",
    )
    print("  ok   case3: flag ON + no ACCEPTANCE_TEST line → plain mock reply (harness needs the line)")


# ── Case 4: flag ON + ACCEPTANCE_TEST: false → non-pass + cost logged ────────
def test_case4_harness_on_always_fails_cost_logged():
    """Harness engaged, always-failing test → bounded failure; cost ledger gets a row.

    Gap-2 proof: the lane returns NORMALLY (structured __HARNESS__: result) rather
    than timing out at 600s, and its cost appears in the ledger (the existing
    cost-logging block runs on the normal-return path).
    """
    prompt = "Do something.\nACCEPTANCE_TEST: false\nEnd."
    # Use mkdtemp (not TemporaryDirectory context-manager) so the dir still exists
    # after run_one_task completes and we can inspect the ledger file outside the
    # context-manager scope.
    import shutil
    cwd = tempfile.mkdtemp()
    try:
        ledger_path = str(Path(cwd) / "test-cost-ledger.jsonl")
        env = {
            **BASE_ENV,
            "CLAUDE_REASONIX_GATEWAY_LANE_HARNESS": "1",
            "CLAUDE_REASONIX_REASONIX_COST_LEDGER": ledger_path,
            "REASONIX_FLEET_LOG_DIR": str(Path(cwd) / "logs"),
            # Reduce maxAttempts to 2 so the test finishes fast (2 × ~1s test).
            "CLAUDE_REASONIX_GATEWAY_LANE_MAX_ATTEMPTS": "2",
        }
        with _EnvCtx(**env):
            mcp = _load_mcp()
            result = _run_task(mcp, prompt, cwd)

        expect(result.get("ok") is True, f"case4: task must succeed (bounded return), got: {result}")
        stdout = result.get("stdout") or ""
        expect(
            stdout.startswith("__HARNESS__:"),
            f"case4: stdout must start with __HARNESS__ on bounded failure, got: {stdout!r}",
        )
        expect(
            not stdout.startswith("__HARNESS__:pass:"),
            f"case4: stdout must NOT be __HARNESS__:pass: (should be stagnated/exhausted), got: {stdout!r}",
        )
        # Cost ledger must have at least 1 row (Gap-2: cost logging on bounded-failure path).
        ledger_exists = Path(ledger_path).exists()
        expect(
            ledger_exists,
            f"case4: cost ledger must exist at {ledger_path} (cost logging on bounded failure)",
        )
        rows = [json.loads(line) for line in Path(ledger_path).read_text().splitlines() if line.strip()]
        expect(
            len(rows) >= 1,
            f"case4: cost ledger must have >=1 row after bounded failure, got {len(rows)} rows",
        )
        print(f"  ok   case4: flag ON + ACCEPTANCE_TEST:false → {stdout[:60]!r}")
        print(f"         cost ledger: {len(rows)} row(s) at {ledger_path}")
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


def main() -> int:
    print("test-mcp-harness-wiring: running 4 cases...")

    # Case 4 (always-fails harness with 2 attempts + 120s each test timeout) is the
    # most expensive: 2 × (mock-DeepSeek ~instant + execSync('false') ~instant) but
    # requires 2 shim subprocess round-trips. Still fast (<10s). The test gives 300s.
    test_case1_harness_on_passing()
    test_case2_flag_off_byte_inert()
    test_case3_flag_on_no_acceptance_line()
    test_case4_harness_on_always_fails_cost_logged()

    print("\nPASS: all 4 cases — harness wired through MCP fan-out path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
