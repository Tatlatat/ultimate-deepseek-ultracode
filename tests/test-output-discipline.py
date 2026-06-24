#!/usr/bin/env python3
"""Lever F — OUTPUT DISCIPLINE unit test.

Covers the two layers F adds on top of the Task-2 lane-type/maxOutputTokens
substrate:
  (a) output_discipline_directive(): "" when the flag is OFF; a non-empty block
      carrying the narration-ban + diff-only text when ON.
  (b) output_discipline_budget(lane_type): the HARD layer — maps
      read->512, edit->EDIT budget, everything else (unknown/review/...)->2048,
      and returns None when the flag is OFF (so the gateway passes no cap and
      behavior is unchanged — F defaults OFF).

The gateway module filename is hyphenated, so load it by path.
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

FLAG = "CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE"
EDIT_ENV = "CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE_MAX_TOKENS_EDIT"
READ_ENV = "CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE_MAX_TOKENS_READ"
DEF_ENV = "CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE_MAX_TOKENS_DEFAULT"
DIR_ENV = "CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE_DIRECTIVE"

# The measured EDIT budget default (P95 x 1.2). The ledger had too few real edit
# lanes (3, all describing the edit via StructuredOutput, output_tokens=18), so
# per the brief we use the top-20% proxy 5900 and re-tune once the harness emits
# real edit-format lanes. The test pins the DEFAULT the gateway ships with.
EXPECTED_EDIT_DEFAULT = 5900
EXPECTED_READ = 512
EXPECTED_DEFAULT = 2048

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
    for k in (FLAG, EDIT_ENV, READ_ENV, DEF_ENV, DIR_ENV):
        os.environ.pop(k, None)


def main() -> int:
    # --- (a) directive: OFF by default -> "" -----------------------------------
    _clear_env()
    check(gw.output_discipline_directive() == "",
          "directive is '' when flag unset (F defaults OFF)")

    os.environ[FLAG] = "0"
    check(gw.output_discipline_directive() == "",
          "directive is '' when flag explicitly 0")

    # --- (a) directive: ON -> non-empty narration-ban + diff-only text ---------
    _clear_env()
    os.environ[FLAG] = "1"
    d = gw.output_discipline_directive()
    check(isinstance(d, str) and d.strip() != "",
          "directive is non-empty when flag on")
    low = d.lower()
    check("narration" in low or "i will now" in low,
          "directive carries the narration ban")
    check("diff" in low or "search" in low.replace("research", ""),
          "directive carries the diff-only / SEARCH-REPLACE instruction")
    check("unchanged" in low,
          "directive forbids reprinting unchanged code / '// rest unchanged'")

    # Sub-flag: _DIRECTIVE=0 suppresses ONLY the soft text (budget still applies).
    os.environ[DIR_ENV] = "0"
    check(gw.output_discipline_directive() == "",
          "directive sub-flag _DIRECTIVE=0 suppresses the soft text")

    # --- (b) budget selector: OFF -> None (no cap, unchanged behavior) ----------
    _clear_env()
    check(gw.output_discipline_budget("read") is None,
          "budget is None for read when flag off")
    check(gw.output_discipline_budget("edit") is None,
          "budget is None for edit when flag off")
    check(gw.output_discipline_budget("unknown") is None,
          "budget is None for unknown when flag off")

    # --- (b) budget selector: ON -> read=512, edit=EDIT, unknown=2048 ----------
    _clear_env()
    os.environ[FLAG] = "1"
    check(gw.output_discipline_budget("read") == EXPECTED_READ,
          f"budget read -> {EXPECTED_READ}")
    check(gw.output_discipline_budget("edit") == EXPECTED_EDIT_DEFAULT,
          f"budget edit -> {EXPECTED_EDIT_DEFAULT} (measured P95x1.2 proxy)")
    check(gw.output_discipline_budget("unknown") == EXPECTED_DEFAULT,
          f"budget unknown -> {EXPECTED_DEFAULT}")
    check(gw.output_discipline_budget("review") == EXPECTED_DEFAULT,
          f"budget review (not read/edit) -> {EXPECTED_DEFAULT}")
    check(gw.output_discipline_budget("synthesize") == EXPECTED_DEFAULT,
          f"budget synthesize (not read/edit) -> {EXPECTED_DEFAULT}")

    # --- (b) budgets are env-overridable --------------------------------------
    _clear_env()
    os.environ[FLAG] = "1"
    os.environ[READ_ENV] = "256"
    os.environ[EDIT_ENV] = "4096"
    os.environ[DEF_ENV] = "1024"
    check(gw.output_discipline_budget("read") == 256, "read budget env-overridable")
    check(gw.output_discipline_budget("edit") == 4096, "edit budget env-overridable")
    check(gw.output_discipline_budget("unknown") == 1024, "default budget env-overridable")

    # --- (c) REGRESSION: directives must not poison the classifier --------------
    # Final-matrix bug: F's directive contained 'write'/'apply'. The call-site
    # classifier sees the full assembled prompt (task + directive), so those
    # keywords flipped EVERY lane to 'edit' -> F's per-type cap never applied ->
    # output did not drop. F/A directive text must carry NO _EDIT_INTENT_RE token.
    import re as _re
    _clear_env()
    os.environ[FLAG] = "1"
    os.environ["CLAUDE_REASONIX_GATEWAY_READ_SUMMARY"] = "1"
    _edit_re = _re.compile(
        r"\b(edit|write|create|modify|apply|patch|implement|add|delete|rename|refactor)\b", _re.I)
    _fd = gw.output_discipline_directive()
    _ad = gw.read_lane_summary_instruction("read", None)
    check(not _edit_re.findall(_fd),
          f"F directive carries no edit-intent keyword (would poison classify): {set(w.lower() for w in _edit_re.findall(_fd))}")
    check(not _edit_re.findall(_ad),
          f"A instruction carries no edit-intent keyword: {set(w.lower() for w in _edit_re.findall(_ad))}")
    _read_task = "Read ONLY /x/foo.py and summarize its purpose."
    check(gw.classify_lane_type(None, _read_task + "\n\n" + _fd + "\n\n" + _ad) == "read",
          "read task + F+A directives still classifies 'read' (the cap actually applies)")

    _clear_env()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
