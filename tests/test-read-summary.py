#!/usr/bin/env python3
"""Lever A — READ LANE SUMMARY unit test.

Covers read_lane_summary_instruction():
  (a) Returns "" when CLAUDE_REASONIX_GATEWAY_READ_SUMMARY is OFF (default).
  (b) Returns "" for non-read lane types even when the flag is ON.
  (c) Returns the fixed {findings, files_read, flag} JSON instruction text for
      lane_type=='read' when the flag is ON.
  (d) The instruction contains the three required keys: findings, files_read, flag.
  (e) Is mutually exclusive with a StructuredOutput tool injection: when a
      StructuredOutput tool is present in the payload, the instruction returns "".
  (f) HARD cap: read_summary_budget() returns 512 (default) when ON, None when OFF.
  (g) Env-overridable: CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_MAX_TOKENS overrides 512.
"""
from __future__ import annotations
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "reasonix_native_gateway", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)

FLAG = "CLAUDE_REASONIX_GATEWAY_READ_SUMMARY"
MAX_ENV = "CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_MAX_TOKENS"

_PASS = 0
_FAIL = 0


def check(cond: bool, msg: str) -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {msg}")
    else:
        _FAIL += 1
        print(f"  FAIL {msg}")


def _clear_env() -> None:
    for k in (FLAG, MAX_ENV):
        os.environ.pop(k, None)


def main() -> int:
    # --- (a) OFF by default -> "" for read lane --------------------------------
    _clear_env()
    check(gw.read_lane_summary_instruction("read") == "",
          "returns '' for read lane when flag unset (A defaults OFF)")

    os.environ[FLAG] = "0"
    check(gw.read_lane_summary_instruction("read") == "",
          "returns '' for read lane when flag explicitly 0")
    _clear_env()

    # --- (b) ON but non-read lane type -> "" -----------------------------------
    os.environ[FLAG] = "1"
    check(gw.read_lane_summary_instruction("edit") == "",
          "returns '' for edit lane even when flag ON")
    check(gw.read_lane_summary_instruction("synthesize") == "",
          "returns '' for synthesize lane even when flag ON")
    check(gw.read_lane_summary_instruction("unknown") == "",
          "returns '' for unknown lane even when flag ON")
    _clear_env()

    # --- (c) ON and lane_type=='read' -> non-empty instruction -----------------
    os.environ[FLAG] = "1"
    instr = gw.read_lane_summary_instruction("read")
    check(isinstance(instr, str) and instr.strip() != "",
          "returns non-empty instruction for read lane when flag ON")

    # --- (d) Instruction contains all three required schema keys ---------------
    check("findings" in instr,
          "instruction mentions 'findings' key")
    check("files_read" in instr,
          "instruction mentions 'files_read' key")
    check('"flag"' in instr or "'flag'" in instr or "\"flag\"" in instr,
          "instruction mentions 'flag' key")

    # Instruction should ban raw file contents / prose
    low = instr.lower()
    check("raw" in low or "do not paste" in low or "do not" in low,
          "instruction bans raw file content / prose")
    check("json" in low,
          "instruction requires JSON output")
    _clear_env()

    # --- (e) Mutually exclusive with StructuredOutput tool injection -----------
    # When a StructuredOutput tool is present, instruction must return ""
    os.environ[FLAG] = "1"
    fake_so_tools = [{"name": "StructuredOutput", "input_schema": {"type": "object"}}]
    instr_with_so = gw.read_lane_summary_instruction("read", tools=fake_so_tools)
    check(instr_with_so == "",
          "returns '' for read lane when StructuredOutput tool is already present")
    _clear_env()

    # --- (f) HARD cap: read_summary_budget() returns 512 when ON, None when OFF -
    _clear_env()
    check(gw.read_summary_budget() is None,
          "read_summary_budget() is None when flag OFF")

    os.environ[FLAG] = "1"
    check(gw.read_summary_budget() == 512,
          "read_summary_budget() is 512 (default) when flag ON")

    # --- (g) Env-overridable max tokens ----------------------------------------
    os.environ[MAX_ENV] = "256"
    check(gw.read_summary_budget() == 256,
          "read_summary_budget() respects CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_MAX_TOKENS override")
    _clear_env()

    # --- Summary ---------------------------------------------------------------
    total = _PASS + _FAIL
    print(f"\n{'PASS' if _FAIL == 0 else 'FAIL'}  {_PASS}/{total} checks passed")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
